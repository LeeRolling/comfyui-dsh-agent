# Changelog

首个公开版本基于原作者在 macOS 上的实现；本仓库在其之上补了 Windows 适配与面板上下文修复。

## v0.1.0 — 2026-11-22

首个公开版本（Windows 实机验证 + macOS 路径保留）。

### 新增：Windows 支持

- `dsh_engine.py` 的 `_discover()` 重构为 `_candidate_pairs()`：
  - macOS：`/Applications/DeepSeek Harness.app/Contents/MacOS/DeepSeek Harness` 与 `.../Contents/Resources/app.asar/dsh/node_modules/@deepseek-ai/dsh`
  - Windows：`%LOCALAPPDATA%\Programs\DeepSeek Harness\DeepSeek Harness.exe` 与 `...\resources\app.asar\dsh\node_modules\@deepseek-ai\dsh`
  - 兜底：`ProgramFiles` / `ProgramW6432` / `ProgramFiles(x86)`
  - 只对 exe 做 `os.path.exists()`；asar 是单文件归档，内部路径无法 stat，`bin.js` 交给 Electron 读取
- 仍支持 `DSH_NODE_EXECUTABLE` / `DSH_INSTALL_DIR` 手动覆盖。

### 修复：Windows 上中文变 `?`

- 子进程环境追加 `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`。
- `mcp_server.py` 的 `main()` 对 `sys.stdin` / `sys.stdout` 做 `reconfigure(encoding="utf-8", errors="replace")`，stdout 额外 `newline="\n"`。
- 现象：面板里 agent 的回复出现成片 `?`；排查时容易被误判为节点 bug。

### 修复：面板重复点开关后上下文消失

- 根因一：`buildPanel()` 第一行 `root.innerHTML = ""`，重开面板时把已渲染消息全部清掉。
- 根因二：重开时请求 `/dsh/poll?cursor=-1`，把游标设成「最新事件号」，关闭期间到达的回复被永久跳过。
- 修法：
  - 复用已渲染的 DOM 节点（`S.panelKids`），判据改为「子节点是否仍挂在目标 root 上」，不再无条件清空。
  - 轮询从 `S.cursor` 继续，不再重置到最新。
  - 重开时滚到底部；`startPolling` / `startGraphSync` 先清理旧定时器，避免重复轮询。
- 注意：这只影响可见历史；引擎侧会话一直在，不会因为开关面板而重建。

### 验证环境

- Windows 11 + ComfyUI 0.46.1（frontend 1.59.6）+ DeepSeek Harness Desktop 0.2.2-rc.5，AMD RX 7800XT（ROCm 7.14 / torch 2.12）。
- 验证项：节点加载、`/dsh/status`、面板渲染与截图、`POST /dsh/chat` 完整事件流（tool → tool_result → assistant）、中文往返、侧栏图标位置。
