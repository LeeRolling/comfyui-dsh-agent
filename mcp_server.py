#!/usr/bin/env python3
"""
mcp_server.py -- a dependency-free MCP stdio server that exposes the local
ComfyUI instance to the DeepSeek Harness agent.

It is spawned by DSH (through @deepseek-ai/dsh-mcp-client).  Every tool call
is a thin HTTP request to the ComfyUI custom node's /dsh/* bridge routes, so
this process holds no state and can be probed/restarted freely.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BRIDGE = os.environ.get("COMFY_DSH_BRIDGE", "http://127.0.0.1:8188").rstrip("/")
TIMEOUT = 60

INSTRUCTIONS = """\
你运行在用户本机的 ComfyUI 里，作为「ComfyUI 助手」面板的智能体。

你可以通过 comfy_* 工具直接读取和修改用户当前打开的 ComfyUI 画布：
- comfy_get_context：获取当前画布摘要、最近一次执行报错、队列状态。处理报错或改图前先调用它。
- comfy_get_graph：拿到当前画布的完整 workflow JSON（UI 格式）。
- comfy_replace_graph：提交一份新的 workflow JSON；面板会把它加载到画布（可撤销）。
- comfy_find_nodes / comfy_node_info：查询本机真实安装的节点及其输入输出定义。
- comfy_get_logs：读取 ComfyUI 服务端日志尾部。

工作规则：
1. 用户报错时，先用 comfy_get_context 读取错误与画布，再解释原因并给出可执行的修复。
2. 需要改节点结构时，先 comfy_get_graph，在其基础上做最小修改，再把完整 JSON 交给
   comfy_replace_graph；不要凭记忆编造节点类名，先用 comfy_find_nodes 确认本机存在。
3. 保留用户已有的模型、采样器等设置，除非用户明确要求更换。
4. workflow JSON 必须是 ComfyUI 前端格式（含 nodes / links / last_node_id / last_link_id）。
5. 修改后告诉用户"已提交到画布"，并说明改了哪些节点。
"""

TOOLS = [
    {
        "name": "comfy_get_context",
        "description": (
            "读取 ComfyUI 当前状态：画布节点摘要（id/类型/标题/关键控件值）、"
            "最近一次执行报错的完整信息、队列与系统信息、日志尾部。"
            "处理报错或修改画布前应先调用。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "comfy_get_graph",
        "description": "返回当前 ComfyUI 画布的完整 workflow JSON（前端 UI 格式，可直接用于 comfy_replace_graph）。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "comfy_replace_graph",
        "description": (
            "用一份新的 workflow JSON 替换用户当前画布（前端会执行可撤销的加载）。"
            "必须传完整的前端 UI 格式 JSON：nodes、links、last_node_id、last_link_id。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "graph_json": {"type": "string", "description": "完整的 workflow JSON 字符串"},
                "reason": {"type": "string", "description": "一句话说明为什么这样改，会显示在画布提示里"},
            },
            "required": ["graph_json"],
            "additionalProperties": False,
        },
    },
    {
        "name": "comfy_find_nodes",
        "description": "在本机真实安装的节点中按名称/分类/输入输出类型搜索节点类。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键字，例如 'upscale'、'KSampler'、'视频'"},
                "limit": {"type": "integer", "description": "最多返回条数，默认 25", "minimum": 1, "maximum": 100},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "comfy_node_info",
        "description": "返回一个节点类的完整输入/输出定义（object_info），用于正确连线与填写控件值。",
        "inputSchema": {
            "type": "object",
            "properties": {"class_type": {"type": "string", "description": "节点类名，例如 'KSampler'"}},
            "required": ["class_type"],
            "additionalProperties": False,
        },
    },
    {
        "name": "comfy_get_logs",
        "description": "读取 ComfyUI 服务端日志的最后若干行，用于定位启动/加载/执行错误。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "lines": {"type": "integer", "description": "行数，默认 120，最大 800", "minimum": 10, "maximum": 800}
            },
            "additionalProperties": False,
        },
    },
]


class BridgeError(RuntimeError):
    pass


def http_json(path: str, payload: dict | None = None) -> dict:
    url = BRIDGE + path
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:2000]
        raise BridgeError(f"HTTP {exc.code} {path}: {body}") from exc
    except Exception as exc:  # noqa: BLE001
        raise BridgeError(f"无法连接 ComfyUI 桥接服务（{BRIDGE}{path}）: {exc}") from exc
    try:
        return json.loads(raw)
    except Exception:
        return {"raw": raw}


def text_result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def call_tool(name: str, args: dict) -> dict:
    if name == "comfy_get_context":
        data = http_json("/dsh/state")
        return text_result(data.get("text") or json.dumps(data, ensure_ascii=False)[:20000])

    if name == "comfy_get_graph":
        data = http_json("/dsh/graph")
        graph = data.get("graph")
        if not graph:
            return text_result("当前没有可用的画布快照：请让用户先点一下画布，或确认 ComfyUI 页面已打开。", True)
        return text_result(json.dumps(graph, ensure_ascii=False))

    if name == "comfy_replace_graph":
        raw = args.get("graph_json") or ""
        try:
            graph = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:  # noqa: BLE001
            return text_result(f"graph_json 不是合法 JSON: {exc}", True)
        if not isinstance(graph, dict) or "nodes" not in graph:
            return text_result("graph_json 必须是 ComfyUI 前端格式，至少包含 nodes 字段。", True)
        data = http_json(
            "/dsh/pending",
            {"graph": graph, "reason": args.get("reason") or "Agent 重新规划了节点"},
        )
        return text_result(
            f"已提交到画布：{data.get('nodes', '?')} 个节点、{data.get('links', '?')} 条连线。"
            f"前端会加载并在面板显示「已应用」。"
        )

    if name == "comfy_find_nodes":
        q = urllib.parse.urlencode({"query": args.get("query", ""), "limit": int(args.get("limit", 25))})
        data = http_json("/dsh/find_nodes?" + q)
        return text_result(data.get("text") or "没有找到节点。")

    if name == "comfy_node_info":
        q = urllib.parse.urlencode({"class_type": args.get("class_type", "")})
        data = http_json("/dsh/node_info?" + q)
        return text_result(data.get("text") or "没有找到该节点。", is_error=bool(data.get("error")))

    if name == "comfy_get_logs":
        lines = int(args.get("lines", 120))
        q = urllib.parse.urlencode({"lines": lines})
        data = http_json("/dsh/logs?" + q)
        return text_result(data.get("text") or "")

    raise BridgeError(f"未知工具: {name}")


def handle(msg: dict) -> dict | None:
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": params.get("protocolVersion") or "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "comfyui-bridge", "version": "1.0.0"},
                "instructions": INSTRUCTIONS,
            },
        }
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            result = call_tool(name, args)
        except BridgeError as exc:
            result = text_result(str(exc), True)
        except Exception as exc:  # noqa: BLE001
            result = text_result(f"工具执行失败: {exc}", True)
        return {"jsonrpc": "2.0", "id": req_id, "result": result}
    if method in ("resources/list", "resources/templates/list"):
        return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": [], "resourceTemplates": []}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": []}}
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> None:
    # Windows pipes default to the ANSI code page (cp936 here), which turns
    # every CJK payload into mojibake on the DSH side.  The MCP stream is
    # UTF-8 by spec, so force it before touching stdin/stdout.
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - very old / embedded Python
        pass

    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        try:
            reply = handle(msg)
        except Exception as exc:  # noqa: BLE001
            reply = {
                "jsonrpc": "2.0",
                "id": msg.get("id"),
                "error": {"code": -32603, "message": str(exc)},
            }
        if reply is None:
            continue
        out.write(json.dumps(reply, ensure_ascii=False) + "\n")
        out.flush()


if __name__ == "__main__":
    main()
