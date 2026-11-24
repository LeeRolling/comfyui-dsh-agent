"""会话存储：索引、逐会话事件、回收站、旧记录导入。

本模块只做文件层面的事，不碰 ComfyUI 与 DSH 运行时，便于单独测试。

目录结构（root 默认为 <ComfyUI>/user/default/dsh-agent）：

    index.json                 # 会话索引（标题、时间、条数、状态）
    sessions/<sid>.jsonl       # 逐条事件的明文副本（一行一个 JSON）
    trash/<sid>.jsonl          # 回收站里的事件副本

事件行格式：{"kind":"user","ts":1795...,"text":"..."}

并发：所有公开方法都在同一把 RLock 内执行 —— 事件来自引擎读线程，
而新建/改名/删除来自 aiohttp 的工作线程，二者会同时写 index.json。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Any, Iterable

SESSION_PREFIX = "comfyui-"
MAX_EVENTS_PER_SESSION = 2000
TITLE_LIMIT = 24

# sessionId 会被拼进文件名，必须严格限制，否则 ../ 或绝对路径可逃出 root。
_SID_RE = re.compile(r"^comfyui-[A-Za-z0-9_-]{1,64}$")


def _now() -> float:
    return time.time()


def safe_sid(sid: Any) -> str:
    """校验并返回合法 sessionId；不合法直接抛 ValueError。"""
    if not isinstance(sid, str) or not _SID_RE.match(sid):
        raise ValueError(f"invalid sessionId: {sid!r}")
    return sid


def strip_context(text: str) -> str:
    """去掉面板注入的 <comfy_context> 尾巴，只留用户自己打的话。"""
    raw = text or ""
    for marker in ("<comfy_context", "<comfy_context>"):
        pos = raw.find(marker)
        if pos >= 0:
            raw = raw[:pos]
            break
    return raw.strip()


def default_title(text: str) -> str:
    """从首条用户消息截一个标题。"""
    cleaned = " ".join(strip_context(text).split())
    if not cleaned:
        return "新对话"
    if len(cleaned) <= TITLE_LIMIT:
        return cleaned
    return cleaned[:TITLE_LIMIT] + "…"


class SessionStore:
    def __init__(self, root: str) -> None:
        self.root = root
        self.index_path = os.path.join(root, "index.json")
        self.sessions_dir = os.path.join(root, "sessions")
        self.trash_dir = os.path.join(root, "trash")
        self._index: dict[str, dict] = {}
        self._loaded = False
        self._lock = threading.RLock()

    # -- index -------------------------------------------------------------

    def load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            os.makedirs(self.sessions_dir, exist_ok=True)
            os.makedirs(self.trash_dir, exist_ok=True)
            self._index = self._read_index()
            self._reconcile()
            # 旧版本可能把 <comfy_context> 写进了标题，读入时清理一次
            dirty = False
            for meta in self._index.values():
                title = meta.get("title") or ""
                if "<comfy_context" in title:
                    meta["title"] = default_title(title)
                    dirty = True
            self._loaded = True
            if dirty:
                try:
                    self._save_index()
                except OSError:
                    pass

    def _read_index(self) -> dict[str, dict]:
        """读 index.json；损坏时备份并尽量从 sessions/ 重建。"""
        if not os.path.exists(self.index_path):
            return {}
        try:
            with open(self.index_path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("index.json is not an object")
            return {k: v for k, v in data.items() if isinstance(v, dict)}
        except (OSError, ValueError) as exc:
            try:
                bad = self.index_path + ".bad-" + time.strftime("%Y%m%d%H%M%S")
                shutil.copy2(self.index_path, bad)
            except OSError:
                pass
            print(f"[dsh-agent] index.json 读取失败，将从 sessions/ 重建：{exc}")
            return self._rebuild_index()

    def _rebuild_index(self) -> dict[str, dict]:
        rebuilt: dict[str, dict] = {}
        try:
            names = os.listdir(self.sessions_dir)
        except OSError:
            return rebuilt
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            sid = name[: -len(".jsonl")]
            if not _SID_RE.match(sid):
                continue
            path = os.path.join(self.sessions_dir, name)
            try:
                st = os.stat(path)
                count = 0
                with open(path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        if line.strip():
                            count += 1
            except OSError:
                continue
            rebuilt[sid] = {
                "title": "新对话",
                "createdAt": st.st_ctime,
                "updatedAt": st.st_mtime,
                "count": min(count, MAX_EVENTS_PER_SESSION),
                "customTitle": False,
                "trashed": False,
            }
        return rebuilt

    def _reconcile(self) -> None:
        """清掉文件已不存在的索引条目，避免点开报错 / 幽灵回收站。"""
        for sid in list(self._index.keys()):
            if sid == "__last__":
                continue
            meta = self._index[sid]
            live = os.path.exists(self._events_path(sid))
            gone = os.path.exists(os.path.join(self.trash_dir, f"{sid}.jsonl"))
            if not live and not gone:
                self._index.pop(sid, None)

    def _save_index(self) -> None:
        with self._lock:
            self.load()
            payload = json.dumps(self._index, ensure_ascii=False, indent=2)
            fd, tmp = tempfile.mkstemp(dir=self.root, prefix="index-", suffix=".json.tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                for _ in range(20):
                    try:
                        os.replace(tmp, self.index_path)
                        return
                    except PermissionError:
                        time.sleep(0.05)
                os.replace(tmp, self.index_path)
            finally:
                if os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass

    def entry(self, sid: str) -> dict | None:
        with self._lock:
            self.load()
            return self._index.get(sid)

    def list_sessions(self) -> list[dict]:
        with self._lock:
            self.load()
            rows = []
            for sid, meta in list(self._index.items()):
                if sid == "__last__" or meta.get("trashed"):
                    continue
                if not os.path.exists(self._events_path(sid)):
                    continue
                rows.append({"sessionId": sid, **meta})
            rows.sort(key=lambda r: r.get("updatedAt") or 0, reverse=True)
            return rows

    def list_trash(self) -> list[dict]:
        with self._lock:
            self.load()
            rows = []
            for sid, meta in list(self._index.items()):
                if sid == "__last__" or not meta.get("trashed"):
                    continue
                if not os.path.exists(os.path.join(self.trash_dir, f"{sid}.jsonl")):
                    continue
                rows.append({"sessionId": sid, **meta})
            rows.sort(key=lambda r: r.get("trashedAt") or 0, reverse=True)
            return rows

    def last_session_id(self) -> str | None:
        with self._lock:
            self.load()
            sid = self._index.get("__last__", {}).get("sessionId")
            if sid and self._index.get(sid) and not self._index[sid].get("trashed"):
                return sid
            rows = self.list_sessions()
            return rows[0]["sessionId"] if rows else None

    def set_last(self, sid: str) -> None:
        with self._lock:
            self.load()
            if not _SID_RE.match(sid or ""):
                return
            self._index["__last__"] = {"sessionId": sid}
            self._save_index()

    # -- events ------------------------------------------------------------

    def _events_path(self, sid: str) -> str:
        return os.path.join(self.sessions_dir, safe_sid(sid) + ".jsonl")

    def _trash_path(self, sid: str) -> str:
        return os.path.join(self.trash_dir, safe_sid(sid) + ".jsonl")

    def create(self, sid: str | None = None, title: str | None = None) -> dict:
        with self._lock:
            self.load()
            sid = safe_sid(sid) if sid else SESSION_PREFIX + uuid.uuid4().hex[:12]
            meta = {
                "title": title or "新对话",
                "createdAt": _now(),
                "updatedAt": _now(),
                "count": 0,
                "customTitle": False,
                "trashed": False,
            }
            self._index[sid] = meta
            try:
                open(self._events_path(sid), "a", encoding="utf-8").close()
            except OSError:
                pass
            self._save_index()
            return {"sessionId": sid, **meta}

    def ensure(self, sid: str, title: str | None = None) -> dict:
        with self._lock:
            meta = self.entry(sid)
            if meta:
                return {"sessionId": sid, **meta}
            return self.create(sid, title)

    def rename(self, sid: str, title: str) -> dict | None:
        with self._lock:
            self.load()
            meta = self._index.get(sid)
            if not meta:
                return None
            text = " ".join((title or "").split())[:60]
            if not text:
                return None
            meta["title"] = text
            meta["customTitle"] = True
            meta["updatedAt"] = _now()
            self._save_index()
            return {"sessionId": sid, **meta}

    def append(self, sid: str, event: dict) -> None:
        with self._lock:
            self.load()
            meta = self._index.get(sid)
            if not meta or meta.get("trashed"):
                return
            path = self._events_path(sid)
            line = json.dumps(event, ensure_ascii=False)
            try:
                self._trim_partial_tail(path)
                with open(path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(line + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError:
                return
            meta["updatedAt"] = event.get("ts") or _now()
            if event.get("kind") in ("user", "assistant"):
                meta["count"] = int(meta.get("count") or 0) + 1
                if not meta.get("customTitle"):
                    text = event.get("text") or ""
                    if event.get("kind") == "user" and text:
                        meta["title"] = default_title(text)
            self._save_index()

    @staticmethod
    def _trim_partial_tail(path: str) -> None:
        """上一次崩溃留下半行时先截掉，避免下一条消息与它拼成坏行。"""
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        if not size:
            return
        try:
            with open(path, "rb+") as fh:
                fh.seek(max(0, size - 65536))
                tail = fh.read()
                if tail.endswith(b"\n"):
                    return
                cut = tail.rfind(b"\n")
                keep = 0 if cut < 0 else size - (len(tail) - cut) + 1
                fh.truncate(keep)
        except OSError:
            return

    def read_events(self, sid: str, limit: int = MAX_EVENTS_PER_SESSION) -> list[dict]:
        with self._lock:
            path = self._events_path(sid)
            if not os.path.exists(path):
                return []
            rows: list[dict] = []
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rows.append(json.loads(line))
                        except ValueError:
                            continue
            except OSError:
                return []
            if len(rows) > limit:
                rows = rows[-limit:]
            # 旧版本落盘的用户消息可能带 <comfy_context> 尾巴，回放时去掉
            for row in rows:
                if row.get("kind") == "user" and isinstance(row.get("text"), str):
                    row["text"] = strip_context(row["text"])
            return rows

    # -- trash -------------------------------------------------------------

    def move_to_trash(self, sid: str) -> bool:
        with self._lock:
            self.load()
            meta = self._index.get(sid)
            if not meta or meta.get("trashed"):
                return False
            meta["trashed"] = True
            meta["trashedAt"] = _now()
            try:
                self._save_index()
            except OSError:
                meta["trashed"] = False
                meta.pop("trashedAt", None)
                return False
            src = self._events_path(sid)
            dst = self._trash_path(sid)
            try:
                if os.path.exists(src):
                    os.replace(src, dst)
            except OSError:
                try:
                    shutil.copy2(src, dst)
                    os.unlink(src)
                except OSError:
                    meta["trashed"] = False
                    meta.pop("trashedAt", None)
                    try:
                        self._save_index()
                    except OSError:
                        pass
                    return False
            if sid == (self._index.get("__last__") or {}).get("sessionId"):
                self._index.pop("__last__", None)
                try:
                    self._save_index()
                except OSError:
                    pass
            return True

    def restore(self, sid: str) -> bool:
        with self._lock:
            self.load()
            meta = self._index.get(sid)
            if not meta or not meta.get("trashed"):
                return False
            src = self._trash_path(sid)
            if not os.path.exists(src):
                return False
            dst = self._events_path(sid)
            try:
                os.replace(src, dst)
            except OSError:
                return False
            meta["trashed"] = False
            meta.pop("trashedAt", None)
            self._save_index()
            return True

    def purge(self, sid: str) -> bool:
        """永久删除：只允许删回收站里的条目，避免误删活动会话。"""
        with self._lock:
            self.load()
            meta = self._index.get(sid)
            if not meta or not meta.get("trashed"):
                return False
            for path in (self._events_path(sid), self._trash_path(sid)):
                try:
                    if os.path.exists(path):
                        os.unlink(path)
                except OSError:
                    pass
            self._index.pop(sid, None)
            if sid == (self._index.get("__last__") or {}).get("sessionId"):
                self._index.pop("__last__", None)
            self._save_index()
            return True

    # -- legacy import -----------------------------------------------------

    def import_legacy(self, dsh_sessions_dir: str, slug: str, node_exe: str | None, unpack_js: str) -> int:
        """把 DSH 自己存的旧会话读进来（只读，不改 DSH 目录）。

        返回值：新导入的会话数。已导入过的（索引里存在）跳过。
        """
        self.load()
        base = os.path.join(dsh_sessions_dir, slug)
        if not os.path.isdir(base):
            return 0
        imported = 0
        for name in sorted(os.listdir(base)):
            if not _SID_RE.match(name) or self.entry(name):
                continue
            src = os.path.join(base, name, "session.v4.jsonl.zstd")
            if not os.path.exists(src):
                continue
            try:
                events = self._read_legacy_log(src, node_exe, unpack_js)
            except Exception:
                events = None
            if not events:
                continue
            self.create(name)
            for ev in events:
                self.append(name, ev)
            imported += 1
        return imported

    @staticmethod
    def _read_legacy_log(path: str, node_exe: str | None, unpack_js: str) -> list[dict] | None:
        if not node_exe or not os.path.exists(unpack_js):
            return None
        try:
            with tempfile.TemporaryDirectory(prefix="dsh-legacy-") as tmp:
                out = os.path.join(tmp, "out.jsonl")
                proc = subprocess.run(
                    [node_exe, unpack_js, path, out],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if proc.returncode != 0 or not os.path.exists(out):
                    return None
                with open(out, "r", encoding="utf-8") as fh:
                    lines = [json.loads(x) for x in fh if x.strip()]
        except Exception:
            return None
        return legacy_entries_to_events(lines)


def legacy_entries_to_events(entries: Iterable[dict]) -> list[dict]:
    """把 DSH 的 session.v4.jsonl 条目压成面板能显示的最小事件序列。"""
    events: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        etype = entry.get("type")
        data = entry.get("data") or {}
        try:
            ts = (entry.get("time") or 0) / 1000.0 if entry.get("time") else _now()
        except TypeError:
            ts = _now()
        if etype == "agent/inbox/spliced":
            for item in data.get("inserted") or []:
                text = "".join(
                    block.get("text", "")
                    for block in item.get("content") or []
                    if isinstance(block, dict) and block.get("type") == "text"
                )
                text = _strip_comfy_context(text)
                if text.strip():
                    events.append({"kind": "user", "text": text.strip(), "ts": ts})
        elif etype in ("assistant/message", "message/assistant"):
            blocks = (data.get("message") or {}).get("content") or data.get("content") or []
            text = "".join(
                block.get("text", "")
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "text"
            )
            if text.strip():
                events.append({"kind": "assistant", "text": text.strip(), "ts": ts})
        elif etype == "tool/call":
            events.append(
                {
                    "kind": "tool",
                    "name": data.get("name") or "tool",
                    "args": str(data.get("arguments") or "")[:1200],
                    "ts": ts,
                }
            )
    for i, ev in enumerate(events):
        ev["seq"] = i + 1
    return events


def _strip_comfy_context(text: str) -> str:
    marker = "<comfy_context>"
    if marker in text:
        text = text.split(marker, 1)[0]
    return text.rstrip()
