"""
dsh_engine.py -- a tiny, dependency-free client for the DeepSeek Harness
"SDK" profile (stdio JSON-RPC 2.0).

The ComfyUI custom node runs inside ComfyUI's Python process.  This module
spawns a dedicated DSH runtime as a child process and speaks the documented
SDK wire protocol with it:

    initialize -> session/prompt -> session.event / session.status ...

Nothing here imports DSH packages; the wire format is newline-delimited
JSON-RPC, so plain ``subprocess`` + ``json`` is enough.

v2: one runtime process, many conversations.  Every session keeps its own
event buffer; the panel switches between them and the transcripts are
persisted by ``dsh_store`` so a restart does not lose the visible history.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections import deque

# --------------------------------------------------------------------------
# Locating the DeepSeek Harness installation
# --------------------------------------------------------------------------

_DSH_PKG_REL = os.path.join("resources", "app.asar", "dsh", "node_modules", "@deepseek-ai", "dsh")


def _candidate_pairs() -> list[tuple[str, str]]:
    """(electron executable, dsh CLI package dir) pairs, most likely first."""
    pairs: list[tuple[str, str]] = []

    # macOS packaged app.
    bundle = "/Applications/DeepSeek Harness.app"
    pairs.append(
        (
            os.path.join(bundle, "Contents", "MacOS", "DeepSeek Harness"),
            os.path.join(bundle, "Contents", "Resources", "app.asar", "dsh",
                         "node_modules", "@deepseek-ai", "dsh"),
        )
    )

    # Windows packaged installs (per-user first, then machine-wide).
    roots: list[str] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(os.path.join(local, "Programs", "DeepSeek Harness"))
    for key in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        base = os.environ.get(key)
        if base:
            roots.append(os.path.join(base, "DeepSeek Harness"))
    for root in roots:
        pairs.append(
            (
                os.path.join(root, "DeepSeek Harness.exe"),
                os.path.join(root, _DSH_PKG_REL),
            )
        )

    return pairs


def _discover() -> tuple[str | None, str | None]:
    """Return (electron_executable, dsh_cli_package_dir)."""
    exe = os.environ.get("DSH_NODE_EXECUTABLE")
    inst = os.environ.get("DSH_INSTALL_DIR")
    if exe and inst:
        return exe, inst
    for cand_exe, cand_inst in _candidate_pairs():
        # The package dir lives inside app.asar, which Python cannot stat on
        # any platform; only the outer executable is probed.  Electron itself
        # reads bin.js straight out of the archive.
        if os.path.exists(cand_exe):
            return cand_exe, cand_inst
    return None, None


# --------------------------------------------------------------------------
# Runtime
# --------------------------------------------------------------------------

MAX_EVENTS = 800


class DshError(RuntimeError):
    pass


class DshRuntime:
    """Owns one DSH SDK runtime process and many conversations."""

    def __init__(
        self,
        cwd: str,
        bridge_url: str,
        mcp_server_path: str,
        python_exe: str | None = None,
        provider: str = "deepseek-account",
        model: str = "deepseek-flash",
        reasoning_effort: str = "high",
        store=None,
    ) -> None:
        self.cwd = cwd
        self.bridge_url = bridge_url
        self.mcp_server_path = mcp_server_path
        self.python_exe = python_exe or sys.executable
        self.provider = provider
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.store = store

        self.status = "stopped"  # stopped | starting | ready | error
        self.last_error = ""

        self._proc: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pending: dict[int, dict] = {}
        self._next_id = 1
        self._ready_event = threading.Event()
        self._stderr_tail: deque[str] = deque(maxlen=80)
        self._closing = False
        self._pending_questions: dict[str, dict] = {}
        self._pending_approvals: dict[str, dict] = {}
        self._auto_approve: dict[str, bool] = {}
        self._patch_path: str | None = None

        # sid -> {"events": [], "seq": int, "running": bool,
        #         "turn_had_text": bool, "turn_error": str, "hydrated": bool}
        self._buckets: dict[str, dict] = {}
        self.session_id = self._first_session_id()

    # -- sessions ----------------------------------------------------------

    def _new_sid(self) -> str:
        return "comfyui-" + uuid.uuid4().hex[:12]

    def _first_session_id(self) -> str:
        if self.store is not None:
            try:
                sid = self.store.last_session_id()
                if sid:
                    return sid
            except Exception:
                pass
        return self._new_sid()

    def _bucket(self, sid: str) -> dict:
        bucket = self._buckets.get(sid)
        if bucket is None:
            bucket = {
                "events": [],
                "seq": 0,
                "running": False,
                "turn_had_text": False,
                "turn_error": "",
                "hydrated": False,
            }
            self._buckets[sid] = bucket
        return bucket

    def _hydrate(self, sid: str) -> None:
        """Load the persisted transcript into this in-memory bucket, once."""
        bucket = self._bucket(sid)
        if bucket["hydrated"]:
            return
        bucket["hydrated"] = True
        if self.store is None:
            return
        try:
            rows = self.store.read_events(sid)
        except Exception:
            rows = []
        for row in rows:
            bucket["seq"] += 1
            item = {"seq": bucket["seq"], "ts": row.get("ts") or time.time()}
            item.update({k: v for k, v in row.items() if k not in ("seq", "ts")})
            bucket["events"].append(item)

    def list_sessions(self) -> list[dict]:
        if self.store is None:
            return []
        rows = self.store.list_sessions()
        for row in rows:
            sid = row.get("sessionId")
            bucket = self._buckets.get(sid)
            if bucket is not None:
                row["running"] = bool(bucket.get("running"))
        return rows

    def list_trash(self) -> list[dict]:
        return self.store.list_trash() if self.store is not None else []

    def rename_session(self, sid: str, title: str) -> dict | None:
        if self.store is None:
            return None
        return self.store.rename(sid, title)

    def trash_session(self, sid: str) -> dict:
        if self.store is None:
            return {"ok": False, "error": "no store"}
        if sid == self.session_id:
            # 当前会话被丢进回收站：先切到别的会话，避免界面对着已删除的 id
            remaining = [r["sessionId"] for r in self.store.list_sessions() if r["sessionId"] != sid]
            ok = self.store.move_to_trash(sid)
            nxt = remaining[0] if remaining else None
            if nxt:
                try:
                    self.activate(nxt)
                except Exception:
                    pass
            else:
                self._reset_active()
            return {"ok": ok, "next": nxt}
        return {"ok": self.store.move_to_trash(sid)}

    def restore_session(self, sid: str) -> dict:
        if self.store is None:
            return {"ok": False}
        return {"ok": self.store.restore(sid)}

    def purge_session(self, sid: str) -> dict:
        if self.store is None:
            return {"ok": False}
        # store.purge() 只删回收站里的条目；先确认成功再丢内存桶，
        # 否则一次误调用会把活动会话的内存历史也清掉。
        try:
            ok = self.store.purge(sid)
        except ValueError:
            return {"ok": False, "error": "invalid sessionId"}
        if ok:
            self._buckets.pop(sid, None)
        return {"ok": ok}

    def _reset_active(self) -> str:
        sid = self._new_sid()
        with self._state_lock:
            self.session_id = sid
            self._bucket(sid)
        if self.store is not None:
            try:
                self.store.create(sid)
                self.store.set_last(sid)
            except Exception:
                pass
        return sid

    def _set_active(self, sid: str) -> None:
        """只切"当前会话"这个指针，不碰 runtime（避免后台会话被 activate 抢走）。"""
        with self._state_lock:
            self.session_id = sid
            self._bucket(sid)
        self._hydrate(sid)
        if self.store is not None:
            try:
                self.store.set_last(sid)
            except Exception:
                pass

    def _prepare_session(self, sid: str) -> None:
        """确保会话已在 runtime 里 resume 过；历史事件不会重放，只挂上新的。"""
        bucket = self._bucket(sid)
        if not bucket.get("hydrated"):
            self._hydrate(sid)
        if bucket.get("resumed"):
            return
        try:
            self._call("session/resume", {"sessionId": sid}, timeout=30.0)
        except DshError:
            # 会话第一次使用时并不存在，runtime 会在第一次 prompt 时创建。
            pass
        bucket["resumed"] = True

    def activate(self, sid: str) -> dict:
        """Switch the active conversation; resume it in the SDK runtime."""
        if not sid:
            raise DshError("sessionId 为空")
        if self.store is not None:
            try:
                self.store.ensure(sid)
            except Exception:
                pass
        with self._state_lock:
            self.session_id = sid
            self._bucket(sid)
        self._hydrate(sid)
        if self.store is not None:
            try:
                self.store.set_last(sid)
            except Exception:
                pass
        try:
            self.ensure_started()
            # The runtime keeps every session alive; resume only re-attaches
            # persisted state after a restart (history is not replayed).
            self._call("session/resume", {"sessionId": sid}, timeout=30.0)
            self._bucket(sid)["resumed"] = True
        except DshError:
            # 会话第一次使用时并不存在，runtime 会在第一次 prompt 时创建；
            # 这里吞掉 resume 失败，真正的错误留给 submit 报。
            pass
        return {"sessionId": sid, "status": self.status}

    def new_session(self, title: str | None = None) -> dict:
        if self.store is not None:
            try:
                self.ensure_started()
            except DshError:
                raise
            row = self.store.create(title=title)
            sid = row["sessionId"]
        else:
            sid = self._new_sid()
        with self._state_lock:
            self.session_id = sid
            self._bucket(sid)
            self._bucket(sid)["hydrated"] = True
        if self.store is not None:
            try:
                self.store.set_last(sid)
            except Exception:
                pass
        return {"sessionId": sid}

    # -- lifecycle ---------------------------------------------------------

    def ensure_started(self, timeout: float = 90.0) -> None:
        started_here = False
        with self._state_lock:
            if self.status == "ready":
                return
            if self.status == "starting":
                started_here = False
            else:
                self.status = "starting"
                self.last_error = ""
                started_here = True
                # 上一次运行时崩溃/被 reset 后这个 event 还停在 set 状态，
                # 不清掉的话下面的 wait() 会立刻返回，然后误报"启动失败"。
                self._ready_event.clear()
                self._spawn_locked()
        if started_here:
            self._start_threads()
            threading.Thread(target=self._initialize, daemon=True).start()
        if not self._ready_event.wait(timeout):
            raise DshError(self.last_error or "DeepSeek Harness runtime 启动超时")
        if self.status != "ready":
            raise DshError(self.last_error or "DeepSeek Harness runtime 启动失败")

    def _write_patch(self) -> str:
        patch = f"""- insert:
    - id: comfyui-bridge-mcp
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: comfy
        transport: stdio
        command: {json.dumps(self.python_exe)}
        args: [{json.dumps(self.mcp_server_path)}]
        env:
          COMFY_DSH_BRIDGE: {json.dumps(self.bridge_url)}
        toolCallTimeoutMs: 180000
        startupTimeoutMs: 30000
"""
        import tempfile

        fd, path = tempfile.mkstemp(prefix="dsh-comfy-patch-", suffix=".yml")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(patch)
        self._patch_path = path
        return path

    def _spawn_locked(self) -> None:
        # 旧进程（崩溃遗留或上一次重置）必须先收掉：否则两个 runtime 会同时
        # 写同一个会话，旧读线程还会把 status 覆盖回去。
        old = self._proc
        if old is not None and old.poll() is None:
            try:
                old.terminate()
                old.wait(timeout=5)
            except Exception:
                try:
                    old.kill()
                except Exception:
                    pass
        self._proc = None

        exe, install = _discover()
        if not exe or not install:
            self.status = "error"
            self.last_error = (
                "找不到 DeepSeek Harness 安装。请安装 DeepSeek Harness Desktop，"
                "或设置 DSH_NODE_EXECUTABLE / DSH_INSTALL_DIR 环境变量。"
            )
            self._ready_event.set()
            return

        bin_js = os.path.join(install, "lib", "bin.js")
        patch = self._write_patch()
        cmd = [exe, bin_js, "--profile", "sdk", "--patch", patch]

        env = dict(os.environ)
        env["ELECTRON_RUN_AS_NODE"] = "1"
        env["COMFY_DSH_BRIDGE"] = self.bridge_url
        # The MCP child inherits this env; on Windows its stdio would
        # otherwise fall back to the ANSI code page and mangle CJK.
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env.pop("DSH_PROFILE", None)

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=self.cwd,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            self.status = "error"
            self.last_error = f"无法启动 DeepSeek Harness: {exc}"
            self._ready_event.set()

    def _start_threads(self) -> None:
        if self._proc is None:
            return
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_thread.start()

    def _read_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        for line in self._proc.stderr:
            line = line.rstrip()
            if line:
                self._stderr_tail.append(line)

    # -- transport ---------------------------------------------------------

    def _send(self, payload: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise DshError("DeepSeek Harness runtime 未运行")
        data = json.dumps(payload, ensure_ascii=False) + "\n"
        with self._write_lock:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()

    def _call(self, method: str, params=None, timeout: float = 120.0):
        if self._proc is None:
            raise DshError("DeepSeek Harness runtime 未运行")
        with self._state_lock:
            req_id = self._next_id
            self._next_id += 1
            slot = {"event": threading.Event(), "result": None, "error": None}
            self._pending[req_id] = slot
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            self._send(payload)
        except Exception:
            with self._state_lock:
                self._pending.pop(req_id, None)
            raise
        if not slot["event"].wait(timeout):
            with self._state_lock:
                self._pending.pop(req_id, None)
            raise DshError(f"{method} 超时")
        if slot["error"] is not None:
            raise DshError(f"{method}: {slot['error']}")
        return slot["result"]

    def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        for raw in self._proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                self._push("log", text=line[:2000])
                continue
            if not isinstance(msg, dict):
                continue
            if "id" in msg and "method" not in msg:
                with self._state_lock:
                    slot = self._pending.pop(msg["id"], None)
                if slot:
                    if "error" in msg:
                        slot["error"] = msg["error"]
                    else:
                        slot["result"] = msg.get("result")
                    slot["event"].set()
                continue
            if "id" in msg and "method" in msg:
                self._handle_server_request(msg)
                continue
            method = msg.get("method")
            if method:
                self._handle_notification(method, msg.get("params") or {})

        # Process ended
        with self._state_lock:
            was_closing = self._closing
            self.status = "stopped" if was_closing else "error"
            if not was_closing:
                tail = " | ".join(list(self._stderr_tail)[-6:])
                self.last_error = self.last_error or ("DeepSeek Harness runtime 已退出: " + tail)
            for bucket in self._buckets.values():
                bucket["running"] = False
            # 在途请求必须唤醒，否则调用方会白等到 timeout。
            pending = list(self._pending.values())
            self._pending.clear()
        for slot in pending:
            slot["error"] = self.last_error or "runtime 已退出"
            slot["event"].set()
        self._ready_event.set()
        self._push("runtime_stopped", text=self.last_error)

    # -- JSON-RPC server -> client requests --------------------------------

    def _handle_server_request(self, msg: dict) -> None:
        req_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        sid = params.get("sessionId")
        if method == "approval/request":
            tool = params.get("toolName", "tool")
            raw_rid = params.get("requestId")
            # 不用裸 rpc_id 做卡片 id：它是自增整数，前端能猜到 "1"、"2"…
            rid = raw_rid if isinstance(raw_rid, str) and raw_rid.startswith("req_") else "appr_" + uuid.uuid4().hex
            sid = sid or self.session_id
            if self._auto_approve.get(sid):
                self._push("activity", sid=sid, text=f"授权请求 · {tool}（本会话已放行）")
                self._reply(req_id, {"outcome": "allowed-once"})
                return
            self._pending_approvals[rid] = {"rpc_id": req_id, "sid": sid, "tool": tool}
            self._push(
                "approval",
                sid=sid,
                requestId=rid,
                toolName=tool,
                reason=params.get("reason") or "",
                callId=params.get("callId") or "",
            )
            # Do not block the reader thread; timeout rejects after 5 minutes.
            threading.Timer(300, self._timeout_approval, args=(rid,)).start()
        elif method == "user-question/ask":
            raw_qid = params.get("requestId")
            qid = raw_qid if isinstance(raw_qid, str) and raw_qid.startswith("req_") else "ask_" + uuid.uuid4().hex
            sid = sid or self.session_id
            self._pending_questions[qid] = {"rpc_id": req_id, "params": params, "sid": sid}
            self._push(
                "question",
                sid=sid,
                requestId=qid,
                questions=params.get("questions") or [],
            )
            # Do not block the reader; a watchdog cancels after 5 minutes.
            threading.Timer(300, self._cancel_question, args=(qid,)).start()
        else:
            self._reply_error(req_id, -32601, f"unsupported method {method}")

    def _reply(self, req_id, result) -> None:
        try:
            self._send({"jsonrpc": "2.0", "id": req_id, "result": result})
        except Exception:
            pass

    def _reply_error(self, req_id, code, message) -> None:
        try:
            self._send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})
        except Exception:
            pass

    def answer_question(self, question_id: str, answers: list[dict]) -> None:
        with self._state_lock:
            pending = self._pending_questions.pop(question_id, None)
        if not pending:
            return
        self._reply(pending["rpc_id"], {"answers": answers})

    def _cancel_question(self, question_id: str) -> None:
        with self._state_lock:
            pending = self._pending_questions.pop(question_id, None)
        if pending:
            self._reply(pending["rpc_id"], {"cancelled": True})
            self._push("question_closed", sid=pending.get("sid"), requestId=question_id)

    # -- approvals ---------------------------------------------------------

    def resolve_approval(self, request_id: str, outcome: str, remember: bool = False) -> dict:
        if outcome not in ("allowed-once", "rejected", "cancelled"):
            raise DshError("非法授权结果")
        with self._state_lock:
            pending = self._pending_approvals.pop(request_id, None)
        if not pending:
            return {"ok": False, "error": "no such approval"}
        sid = pending.get("sid")
        if remember and outcome == "allowed-once":
            self._auto_approve[sid] = True
            self._push("activity", sid=sid, text=f"授权策略 · 本会话自动放行（{pending.get('tool')}）")
        self._reply(pending["rpc_id"], {"outcome": outcome})
        self._push("approval_closed", sid=sid, requestId=request_id, outcome=outcome)
        return {"ok": True, "outcome": outcome, "remember": bool(remember and outcome == "allowed-once")}

    def auto_approve(self, sid: str | None = None) -> bool:
        return bool(self._auto_approve.get(sid or self.session_id))

    def set_auto_approve(self, enabled: bool, sid: str | None = None) -> dict:
        sid = sid or self.session_id
        self._auto_approve[sid] = bool(enabled)
        # 打开开关时，把当前挂着的请求一并放行，避免卡片一直悬着
        if enabled:
            for rid, pending in list(self._pending_approvals.items()):
                if pending.get("sid") == sid:
                    self.resolve_approval(rid, "allowed-once", remember=False)
        return {"ok": True, "sessionId": sid, "autoApprove": bool(enabled)}

    def _timeout_approval(self, request_id: str) -> None:
        with self._state_lock:
            pending = self._pending_approvals.pop(request_id, None)
        if not pending:
            return
        self._reply(pending["rpc_id"], {"outcome": "rejected"})
        self._push(
            "approval_closed",
            sid=pending.get("sid"),
            requestId=request_id,
            outcome="rejected",
            text="5 分钟未处理，已按拒绝处理",
        )

    def _flush_pending(self, sid: str) -> None:
        """用户在这个会话里又发了新消息：别让旧的问题卡/授权卡拦住它。

        授权一律按"拒绝"处理 —— 用户没点过按钮，就不能替他把危险工具放行。
        """
        with self._state_lock:
            questions = [qid for qid, p in self._pending_questions.items() if p.get("sid") == sid]
            approvals = [rid for rid, p in self._pending_approvals.items() if p.get("sid") == sid]
        for qid in questions:
            self._cancel_question(qid)
        for rid in approvals:
            self.resolve_approval(rid, "rejected", remember=False)
            self._push("activity", sid=sid, text="收到新消息，上一张授权卡已按拒绝处理")

    # -- notifications -----------------------------------------------------

    def _initialize(self) -> None:
        try:
            self._call(
                "initialize",
                {
                    "cwd": self.cwd,
                    "provider": self.provider,
                    "model": self.model,
                    "reasoningEffort": self.reasoning_effort,
                    "approvalRequests": True,
                    "capabilities": {"userQuestions": True},
                },
                timeout=120.0,
            )
            with self._state_lock:
                self.status = "ready"
            self._push("ready", text=f"{self.provider}/{self.model}")
        except Exception as exc:  # noqa: BLE001
            with self._state_lock:
                self.status = "error"
                self.last_error = str(exc)
            self._push("error", text=f"DSH 初始化失败: {exc}")
        finally:
            self._ready_event.set()

    def _handle_notification(self, method: str, params: dict) -> None:
        sid = params.get("sessionId")
        if method == "session.event":
            self._handle_session_event(params.get("event") or {}, sid)
        elif method == "session.status":
            status = params.get("status")
            bucket = self._bucket(sid or self.session_id)
            with self._state_lock:
                bucket["running"] = status == "running"
            self._push("status", sid=sid, status=status)
            if status == "idle" and bucket.get("turn_error"):
                self._push("error", sid=sid, text=bucket["turn_error"])
                bucket["turn_error"] = ""
        elif method == "session.query.completed":
            # 正常情况下 assistant/message 已经把正文流出来了；只有没流到
            # 文本（例如只调了工具就收尾）才用 finalResponse 补一条，避免重复。
            bucket = self._bucket(sid or self.session_id)
            if not bucket.get("turn_had_text"):
                self._push("assistant", sid=sid, text=params.get("finalResponse") or "")

    def _handle_session_event(self, event: dict, sid: str | None = None) -> None:
        etype = event.get("type")
        data = event.get("data") or {}
        sid = data.get("sessionId") or sid
        bucket = self._bucket(sid or self.session_id)
        if etype == "assistant/message":
            message = data.get("message") or {}
            blocks = message.get("content") or []
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            if text.strip():
                bucket["turn_had_text"] = True
                self._push("assistant", sid=sid, text=text)
        elif etype == "tool/call":
            name = data.get("name") or "tool"
            args = data.get("arguments") or ""
            self._push("tool", sid=sid, name=name, callId=data.get("callId"), args=str(args)[:1200])
        elif etype == "tool/result":
            message = data.get("message") or {}
            status = message.get("status") or ("error" if message.get("isError") else "ok")
            preview = ""
            for block in message.get("content") or []:
                if block.get("type") == "text":
                    preview += block.get("text", "")
            self._push(
                "tool_result",
                sid=sid,
                callId=message.get("toolCallId"),
                ok=status != "error",
                preview=preview.strip()[:800],
                error=(data.get("error") or {}).get("reason", ""),
            )
        elif etype == "turn/start":
            bucket["turn_had_text"] = False
        elif etype == "turn/end":
            reason = (data.get("reason") or {}).get("kind")
            bucket["turn_had_text"] = False
            if reason and reason not in ("completed", "max-tokens"):
                bucket["turn_error"] = f"本轮结束原因：{reason}"
        elif etype == "session/title":
            title = data.get("title")
            if title:
                # runtime 用首条消息生成标题，可能把 <comfy_context> 一起截进去
                cut = title.find("<comfy_context")
                if cut >= 0:
                    title = title[:cut].strip() or title
                self._push("title", sid=sid, text=title)
        elif etype == "approval/asked":
            self._push("activity", sid=sid, text="等待授权 …")

    # -- public API --------------------------------------------------------

    def _push(self, kind: str, sid: str | None = None, **fields) -> None:
        sid = sid or self.session_id
        bucket = self._bucket(sid)
        with self._state_lock:
            bucket["seq"] += 1
            item = {"seq": bucket["seq"], "kind": kind, "ts": time.time(), "sessionId": sid}
            item.update(fields)
            bucket["events"].append(item)
            if len(bucket["events"]) > MAX_EVENTS:
                del bucket["events"][:200]
        if self.store is not None and kind in ("user", "assistant"):
            try:
                payload = {"kind": kind, "ts": item["ts"]}
                if "text" in item:
                    payload["text"] = item["text"]
                self.store.append(sid, payload)
            except Exception:
                pass

    def poll(self, cursor: int, sid: str | None = None) -> dict:
        sid = sid or self.session_id
        bucket = self._bucket(sid)
        if not bucket["hydrated"]:
            self._hydrate(sid)
        with self._state_lock:
            events = [] if cursor < 0 else [e for e in bucket["events"] if e["seq"] > cursor]
            return {
                "events": events,
                "cursor": bucket["seq"],
                "sessionId": sid,
                "status": self.status,
                "running": bucket["running"],
                "error": self.last_error,
            }

    def submit(self, text: str, sid: str | None = None) -> dict:
        text = (text or "").strip()
        if not text:
            raise DshError("消息为空")
        sid = sid or self.session_id
        if sid != self.session_id:
            self._set_active(sid)
        self.ensure_started()
        self._flush_pending(sid)
        # 重启后 engine.session_id 可能指向一个还没进 runtime 的旧会话：
        # 先补 hydrate + resume，否则 session/prompt 会报 already exists。
        self._prepare_session(sid)
        bucket = self._bucket(sid)
        bucket["turn_had_text"] = False
        bucket["turn_error"] = ""
        # 面板显示/落盘只用用户自己打的话；<comfy_context> 只喂给模型
        shown = text
        cut = text.find("<comfy_context")
        if cut >= 0:
            shown = text[:cut].strip() or text
        self._push("user", sid=sid, text=shown)
        result = self._call(
            "session/prompt",
            {
                "sessionId": sid,
                "contentBlocks": [{"type": "text", "text": text}],
            },
            timeout=30.0,
        )
        return {"messageId": result.get("messageId") if result else None, "sessionId": sid}

    def stop(self) -> None:
        with self._state_lock:
            self._closing = True
        proc = self._proc
        if proc is None:
            self.status = "stopped"
            return
        try:
            self._call("shutdown", None, timeout=5.0)
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        with self._state_lock:
            self.status = "stopped"
            for bucket in self._buckets.values():
                bucket["running"] = False
            self._proc = None
        if self._patch_path and os.path.exists(self._patch_path):
            try:
                os.unlink(self._patch_path)
            except OSError:
                pass

    def stderr_tail(self) -> list[str]:
        return list(self._stderr_tail)
