"""
dsh_engine.py -- a tiny, dependency-free client for the DeepSeek Harness
"SDK" profile (stdio JSON-RPC 2.0).

The ComfyUI custom node runs inside ComfyUI's Python process.  This module
spawns a dedicated DSH runtime as a child process and speaks the documented
SDK wire protocol with it:

    initialize -> session/prompt -> session.event / session.status ...

Nothing here imports DSH packages; the wire format is newline-delimited
JSON-RPC, so plain ``subprocess`` + ``json`` is enough.
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


class DshError(RuntimeError):
    pass


class DshRuntime:
    """Owns one DSH SDK runtime process and one conversation."""

    def __init__(
        self,
        cwd: str,
        bridge_url: str,
        mcp_server_path: str,
        python_exe: str | None = None,
        provider: str = "deepseek-account",
        model: str = "deepseek-flash",
        reasoning_effort: str = "high",
    ) -> None:
        self.cwd = cwd
        self.bridge_url = bridge_url
        self.mcp_server_path = mcp_server_path
        self.python_exe = python_exe or sys.executable
        self.provider = provider
        self.model = model
        self.reasoning_effort = reasoning_effort

        self.session_id = "comfyui-" + uuid.uuid4().hex[:12]
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
        self._events: list[dict] = []
        self._event_seq = 0
        self._running = False
        self._last_turn_error = ""
        self._stderr_tail: deque[str] = deque(maxlen=80)
        self._closing = False
        self._current_turn_had_text = False
        self._pending_questions: dict[str, dict] = {}
        self._patch_path: str | None = None

    # -- lifecycle ---------------------------------------------------------

    def ensure_started(self, timeout: float = 90.0) -> None:
        with self._state_lock:
            if self.status == "ready":
                return
            if self.status == "starting":
                started_here = False
            else:
                self.status = "starting"
                self.last_error = ""
                started_here = True
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
            self._running = False
        self._ready_event.set()
        self._push("runtime_stopped", text=self.last_error)

    # -- JSON-RPC server -> client requests --------------------------------

    def _handle_server_request(self, msg: dict) -> None:
        req_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "approval/request":
            tool = params.get("toolName", "tool")
            self._push("activity", text=f"授权请求 · {tool}（已自动允许）")
            self._reply(req_id, {"outcome": "allowed-once"})
        elif method == "user-question/ask":
            qid = params.get("requestId") or str(req_id)
            self._pending_questions[qid] = {"rpc_id": req_id, "params": params}
            self._push(
                "question",
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
            self._push("question_closed", requestId=question_id)

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
        if method == "session.event":
            self._handle_session_event(params.get("event") or {})
        elif method == "session.status":
            status = params.get("status")
            with self._state_lock:
                self._running = status == "running"
            self._push("status", status=status)
            if status == "idle" and self._last_turn_error:
                self._push("error", text=self._last_turn_error)
                self._last_turn_error = ""
        elif method == "session.query.completed":
            self._push("assistant", text=params.get("finalResponse") or "")

    def _handle_session_event(self, event: dict) -> None:
        etype = event.get("type")
        data = event.get("data") or {}
        if etype == "assistant/message":
            message = data.get("message") or {}
            blocks = message.get("content") or []
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            if text.strip():
                self._current_turn_had_text = True
                self._push("assistant", text=text)
        elif etype == "tool/call":
            name = data.get("name") or "tool"
            args = data.get("arguments") or ""
            self._push("tool", name=name, callId=data.get("callId"), args=str(args)[:1200])
        elif etype == "tool/result":
            message = data.get("message") or {}
            status = message.get("status") or ("error" if message.get("isError") else "ok")
            preview = ""
            for block in message.get("content") or []:
                if block.get("type") == "text":
                    preview += block.get("text", "")
            self._push(
                "tool_result",
                callId=message.get("toolCallId"),
                ok=status != "error",
                preview=preview.strip()[:800],
                error=(data.get("error") or {}).get("reason", ""),
            )
        elif etype == "turn/end":
            reason = (data.get("reason") or {}).get("kind")
            self._current_turn_had_text = False
            if reason and reason not in ("completed", "max-tokens"):
                self._last_turn_error = f"本轮结束原因：{reason}"
        elif etype == "session/title":
            title = data.get("title")
            if title:
                self._push("title", text=title)
        elif etype == "approval/asked":
            self._push("activity", text="等待授权 …")

    # -- public API --------------------------------------------------------

    def _push(self, kind: str, **fields) -> None:
        with self._state_lock:
            self._event_seq += 1
            item = {"seq": self._event_seq, "kind": kind, "ts": time.time()}
            item.update(fields)
            self._events.append(item)
            if len(self._events) > 800:
                del self._events[:200]

    def poll(self, cursor: int) -> dict:
        with self._state_lock:
            events = [] if cursor < 0 else [e for e in self._events if e["seq"] > cursor]
            return {
                "events": events,
                "cursor": self._event_seq,
                "sessionId": self.session_id,
                "status": self.status,
                "running": self._running,
                "error": self.last_error,
            }

    def submit(self, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            raise DshError("消息为空")
        self.ensure_started()
        self._current_turn_had_text = False
        self._last_turn_error = ""
        result = self._call(
            "session/prompt",
            {
                "sessionId": self.session_id,
                "contentBlocks": [{"type": "text", "text": text}],
            },
            timeout=30.0,
        )
        return {"messageId": result.get("messageId") if result else None, "sessionId": self.session_id}

    def new_session(self) -> str:
        with self._state_lock:
            self.session_id = "comfyui-" + uuid.uuid4().hex[:12]
            self._events = []
            self._event_seq = 0
            self._running = False
            self._last_turn_error = ""
        return self.session_id

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
            self._running = False
            self._proc = None
        if self._patch_path and os.path.exists(self._patch_path):
            try:
                os.unlink(self._patch_path)
            except OSError:
                pass

    def stderr_tail(self) -> list[str]:
        return list(self._stderr_tail)
