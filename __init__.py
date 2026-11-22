"""
ComfyUI custom node: DeepSeek Harness (DSH) agent panel.

Adds a sidebar chat panel plus a small bridge API.  The panel talks to this
node over /dsh/*; the node owns a DeepSeek Harness "SDK" runtime subprocess
and exposes ComfyUI as MCP tools to that runtime.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import traceback
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from dsh_engine import DshError, DshRuntime  # noqa: E402

try:  # ComfyUI's aiohttp server
    from aiohttp import web
    from server import PromptServer
except Exception as exc:  # pragma: no cover - defensive
    print(f"[dsh-agent] ComfyUI server unavailable: {exc}")
    raise

WEB_DIRECTORY = "./web"
NODE_CLASS_MAPPINGS: dict = {}
NODE_DISPLAY_NAME_MAPPINGS: dict = {}
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

LOG_PREFIX = "[dsh-agent]"


# --------------------------------------------------------------------------
# State shared with the panel and with the MCP server
# --------------------------------------------------------------------------


class BridgeState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.graph: dict | None = None
        self.graph_ts: float = 0.0
        self.last_error: dict | None = None
        self.selected: list = []
        self.pending: dict | None = None
        self.pending_ack: float = 0.0
        self.object_info: dict | None = None
        self.object_info_ts: float = 0.0
        self.engine: DshRuntime | None = None
        self.engine_error: str = ""

    # -- graph ---------------------------------------------------------

    def set_graph(self, graph: dict) -> None:
        with self.lock:
            self.graph = graph
            self.graph_ts = time.time()

    def set_error(self, error: dict) -> None:
        with self.lock:
            self.last_error = error

    # -- pending canvas update -----------------------------------------

    def set_pending(self, graph: dict, reason: str) -> dict:
        nodes = graph.get("nodes") if isinstance(graph, dict) else None
        links = graph.get("links") if isinstance(graph, dict) else None
        with self.lock:
            self.pending = {
                "id": uuid.uuid4().hex[:8],
                "graph": graph,
                "reason": reason or "Agent 重新规划了节点",
                "ts": time.time(),
                "nodes": len(nodes or []),
                "links": len(links or []),
            }
            return self.pending

    def take_pending(self) -> dict | None:
        with self.lock:
            return self.pending


STATE = BridgeState()


# --------------------------------------------------------------------------
# Bridge URL / runtime management
# --------------------------------------------------------------------------


def _port() -> int:
    try:
        return int(PromptServer.instance.port)
    except Exception:
        return 8188


def bridge_url() -> str:
    return f"http://127.0.0.1:{_port()}"


def python_exe() -> str:
    return sys.executable or "python3"


def mcp_server_path() -> str:
    return os.path.join(_HERE, "mcp_server.py")


def get_engine() -> DshRuntime:
    with STATE.lock:
        if STATE.engine is None:
            base = _base_dir()
            STATE.engine = DshRuntime(
                cwd=base,
                bridge_url=bridge_url(),
                mcp_server_path=mcp_server_path(),
                python_exe=python_exe(),
                provider=os.environ.get("DSH_COMFY_PROVIDER", "deepseek-account"),
                model=os.environ.get("DSH_COMFY_MODEL", "deepseek-flash"),
                reasoning_effort=os.environ.get("DSH_COMFY_EFFORT", "high"),
            )
        return STATE.engine


# --------------------------------------------------------------------------
# Helpers: log tail, object_info, summaries
# --------------------------------------------------------------------------


def _base_dir() -> str:
    try:
        import folder_paths  # type: ignore

        return folder_paths.base_path
    except Exception:
        return os.path.abspath(os.path.join(_HERE, "..", ".."))


def _find_log_files() -> list[str]:
    base = _base_dir()
    candidates = [
        os.path.join(base, "user", f"comfyui_{_port()}.log"),
        os.path.join(base, "user", "comfyui.log"),
        os.path.join(base, f"comfyui_{_port()}.log"),
        os.path.join(base, "comfyui.log"),
    ]
    user_dir = os.path.join(base, "user")
    if os.path.isdir(user_dir):
        for name in os.listdir(user_dir):
            if name.startswith("comfyui") and name.endswith(".log"):
                candidates.append(os.path.join(user_dir, name))
    found = [p for p in candidates if os.path.isfile(p)]
    found.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return found


def tail_log(lines: int = 120) -> str:
    lines = max(10, min(int(lines or 120), 800))
    files = _find_log_files()
    if not files:
        return "（未找到 ComfyUI 日志文件）"
    path = files[0]
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = min(size, 512 * 1024)
            fh.seek(size - block)
            data = fh.read().decode("utf-8", "replace")
        return f"# {path}\n" + "\n".join(data.splitlines()[-lines:])
    except OSError as exc:
        return f"（读取日志失败: {exc}）"


async def _fetch_object_info(force: bool = False) -> dict:
    with STATE.lock:
        if (
            not force
            and STATE.object_info
            and time.time() - STATE.object_info_ts < 120
        ):
            return STATE.object_info
    import aiohttp

    url = bridge_url() + "/object_info"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            data = await resp.json(content_type=None)
    with STATE.lock:
        STATE.object_info = data
        STATE.object_info_ts = time.time()
    return data


def _short(value, limit: int = 60) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    except Exception:
        text = str(value)
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_graph(graph: dict, max_nodes: int = 60) -> str:
    if not graph:
        return "（尚未收到画布快照：请在 ComfyUI 中打开一个工作流，或点一下画布）"
    nodes = graph.get("nodes") or []
    links = graph.get("links") or []
    lines = [f"画布：{len(nodes)} 个节点，{len(links)} 条连线"]
    for node in nodes[:max_nodes]:
        nid = node.get("id")
        ntype = node.get("type")
        title = node.get("title")
        mode = node.get("mode")
        widgets = node.get("widgets_values")
        extra = ""
        if isinstance(widgets, list) and widgets:
            extra = " | " + ", ".join(_short(v, 36) for v in widgets[:6])
        elif isinstance(widgets, dict) and widgets:
            extra = " | " + ", ".join(f"{k}={_short(v, 36)}" for k, v in list(widgets.items())[:6])
        flags = ""
        if mode == 2:
            flags = " [muted]"
        elif mode == 4:
            flags = " [bypassed]"
        lines.append(f"#{nid} {ntype}" + (f" 「{title}」" if title else "") + flags + extra)
    if len(nodes) > max_nodes:
        lines.append(f"… 其余 {len(nodes) - max_nodes} 个节点省略")
    return "\n".join(lines)


def describe_error(error: dict | None) -> str:
    if not error:
        return "最近没有捕获到执行报错。"
    parts = []
    ts = error.get("ts")
    if ts:
        parts.append(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)))
    node = error.get("node_type") or ""
    nid = error.get("node_id")
    if node or nid:
        parts.append(f"节点 #{nid} {node}".strip())
    head = " · ".join(parts)
    body = [
        ("异常类型", error.get("exception_type")),
        ("异常消息", error.get("exception_message")),
        ("提示", error.get("message")),
    ]
    lines = [f"最近报错（{head}）" if head else "最近报错"]
    for label, value in body:
        if value:
            lines.append(f"{label}: {value}")
    tb = error.get("traceback") or ""
    if tb:
        lines.append("堆栈（尾部）:")
        lines.append("\n".join(str(tb).splitlines()[-25:]))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


routes = PromptServer.instance.routes


@routes.get("/dsh/status")
async def dsh_status(request):
    engine = get_engine()
    return web.json_response(
        {
            "ok": True,
            "engine": engine.status,
            "error": engine.last_error,
            "sessionId": engine.session_id,
            "bridge": bridge_url(),
            "hasGraph": STATE.graph is not None,
        }
    )


@routes.post("/dsh/chat")
async def dsh_chat(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)
    text = (body.get("text") or "").strip()
    if not text:
        return web.json_response({"ok": False, "error": "消息为空"}, status=400)

    context = f"<comfy_context>\n{describe_graph(STATE.graph, 40)}\n{describe_error(STATE.last_error)}\n</comfy_context>"
    payload = f"{text}\n\n{context}"
    engine = get_engine()
    try:
        result = await asyncio.to_thread(engine.submit, payload)
    except DshError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=503)
    except Exception as exc:  # noqa: BLE001
        return web.json_response({"ok": False, "error": f"{exc}"}, status=500)
    return web.json_response({"ok": True, **result, "engine": engine.status})


@routes.get("/dsh/poll")
async def dsh_poll(request):
    try:
        cursor = int(request.query.get("cursor", "0"))
    except ValueError:
        cursor = 0
    engine = get_engine()
    data = engine.poll(cursor)
    pending = STATE.take_pending()
    if pending:
        data["pendingGraph"] = {
            "id": pending["id"],
            "graph": pending["graph"],
            "reason": pending["reason"],
            "nodes": pending["nodes"],
            "links": pending["links"],
        }
    return web.json_response(data)


@routes.post("/dsh/answer")
async def dsh_answer(request):
    body = await request.json()
    request_id = body.get("requestId")
    answers = body.get("answers") or []
    get_engine().answer_question(request_id, answers)
    return web.json_response({"ok": True})


@routes.post("/dsh/reset")
async def dsh_reset(request):
    with STATE.lock:
        engine = STATE.engine
        STATE.engine = None
    if engine is not None:
        await asyncio.to_thread(engine.stop)
    return web.json_response({"ok": True})


@routes.get("/dsh/graph")
async def dsh_graph(request):
    return web.json_response({"graph": STATE.graph, "ts": STATE.graph_ts})


@routes.post("/dsh/graph")
async def dsh_set_graph(request):
    body = await request.json()
    graph = body.get("graph")
    if not isinstance(graph, dict):
        return web.json_response({"ok": False, "error": "graph required"}, status=400)
    STATE.set_graph(graph)
    return web.json_response({"ok": True})


@routes.post("/dsh/error")
async def dsh_error(request):
    body = await request.json()
    body["ts"] = time.time()
    STATE.set_error(body)
    return web.json_response({"ok": True})


@routes.post("/dsh/selection")
async def dsh_selection(request):
    body = await request.json()
    STATE.selected = body.get("nodes") or []
    return web.json_response({"ok": True})


@routes.post("/dsh/pending")
async def dsh_pending_post(request):
    body = await request.json()
    graph = body.get("graph")
    if not isinstance(graph, dict) or "nodes" not in graph:
        return web.json_response({"ok": False, "error": "graph required"}, status=400)
    pending = STATE.set_pending(graph, body.get("reason") or "")
    return web.json_response({"ok": True, "id": pending["id"], "nodes": pending["nodes"], "links": pending["links"]})


@routes.post("/dsh/pending/ack")
async def dsh_pending_ack(request):
    body = await request.json()
    with STATE.lock:
        if STATE.pending and STATE.pending.get("id") == body.get("id"):
            STATE.pending = None
            STATE.pending_ack = time.time()
    return web.json_response({"ok": True})


@routes.get("/dsh/state")
async def dsh_state(request):
    lines: list[str] = []
    lines.append("== 画布 ==")
    lines.append(describe_graph(STATE.graph, 60))
    if STATE.selected:
        lines.append("")
        lines.append("== 用户选中的节点 ==")
        lines.append(json.dumps(STATE.selected, ensure_ascii=False)[:2000])
    lines.append("")
    lines.append("== 报错 ==")
    lines.append(describe_error(STATE.last_error))
    lines.append("")
    lines.append("== 服务 ==")
    try:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.get(bridge_url() + "/system_stats", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                stats = await resp.json(content_type=None)
            async with session.get(bridge_url() + "/queue", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                queue = await resp.json(content_type=None)
        comfy = stats.get("system", {}) if isinstance(stats, dict) else {}
        lines.append(f"ComfyUI {comfy.get('comfyui_version', '?')} · Python {comfy.get('python_version', '?')}")
        running = len(queue.get("queue_running") or [])
        pending_n = len(queue.get("queue_pending") or [])
        lines.append(f"队列：running={running}, pending={pending_n}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"（读取队列/系统信息失败: {exc}）")
    lines.append("")
    lines.append("== 日志尾部 ==")
    lines.append(tail_log(80))
    text = "\n".join(lines)
    return web.json_response({"text": text, "hasGraph": STATE.graph is not None})


@routes.get("/dsh/logs")
async def dsh_logs(request):
    try:
        lines = int(request.query.get("lines", "120"))
    except ValueError:
        lines = 120
    return web.json_response({"text": tail_log(lines)})


@routes.get("/dsh/find_nodes")
async def dsh_find_nodes(request):
    query = (request.query.get("query") or "").strip().lower()
    try:
        limit = int(request.query.get("limit", "25"))
    except ValueError:
        limit = 25
    limit = max(1, min(limit, 100))
    info = await _fetch_object_info()
    hits = []
    for name, spec in info.items():
        category = ""
        if isinstance(spec, dict):
            category = str(spec.get("category") or "")
        hay = f"{name} {category}".lower()
        if query and query not in hay:
            continue
        display = (spec.get("display_name") if isinstance(spec, dict) else "") or name
        hits.append(f"{name}  ({display})  [category: {category}]")
        if len(hits) >= limit:
            break
    text = "\n".join(hits) if hits else "没有匹配的节点。"
    return web.json_response({"text": text, "count": len(hits)})


@routes.get("/dsh/node_info")
async def dsh_node_info(request):
    class_type = request.query.get("class_type") or ""
    info = await _fetch_object_info()
    spec = info.get(class_type)
    if spec is None:
        return web.json_response({"error": True, "text": f"本机没有节点类 {class_type!r}。"})
    return web.json_response({"text": json.dumps(spec, ensure_ascii=False, indent=1)[:24000]})


@routes.get("/dsh/debug")
async def dsh_debug(request):
    engine = get_engine()
    return web.json_response(
        {
            "engine": engine.status,
            "error": engine.last_error,
            "stderr": engine.stderr_tail(),
        }
    )


print(f"{LOG_PREFIX} loaded · bridge {bridge_url()} · mcp {mcp_server_path()}")


def _shutdown() -> None:
    try:
        with STATE.lock:
            engine = STATE.engine
        if engine is not None:
            engine.stop()
    except Exception:
        pass


import atexit  # noqa: E402

atexit.register(_shutdown)
