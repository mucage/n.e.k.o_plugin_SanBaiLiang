# 此地无银三百两账本（sanbailiang）

让 Project N.E.K.O.（猫娘计划）在对话中通过 `@llm_tool` 调用本机「此地无银三百两」账本的 MCP 服务。插件只负责与本机 `MucageLedger.exe --mcp` 通信，**不直接读写 `ledger.json`**，余额锚点、合并、删除墓碑等逻辑全部复用账本软件。

- 仓库：https://github.com/mucage/n.e.k.o_plugin_SanBaiLiang
- 插件 ID：`sanbailiang`（Market 要求全小写，`^[a-z][a-z0-9_]*$`）
- 入口类：`plugin.plugins.sanbailiang:SanbailiangPlugin`
- 依赖：仅 Python 标准库（`asyncio` / `subprocess` / `json`），不需要 `mcp` 等第三方包。
- 通信：本机 stdio，每行一条 JSON-RPC 2.0 消息。

> GitHub 仓库名 `n.e.k.o_plugin_SanBaiLiang` 与插件 ID `sanbailiang` 在大小写不敏感比较下匹配（`n.e.k.o_plugin_sanbailiang`）。

## 提供的工具

| 工具 | 作用 | 什么时候调用 |
| --- | --- | --- |
| `mucage_get_balance` | 查余额、锚点、账目数量 | 用户问“我还有多少钱 / 余额多少” |
| `mucage_set_balance` | 设置总余额（元），以今天 0 点为新锚点 | 用户明确说“把余额改成 / 设为 X 元” |
| `mucage_list_categories` | 列出支出/收入分类 | 记账前确认分类名，或用户问“有哪些分类” |
| `mucage_list_records` | 查账目，可按日期、类型、分类、关键词过滤 | 用户问“最近记了什么 / 查某天的账” |
| `mucage_add_record` | 记一笔支出或收入 | 用户说“记一笔 / 花了 X 元 / 收到工资 X 元” |
| `mucage_get_stats` | 本月或自定义区间的统计、分类占比、每日序列、前几大支出 | 用户问“这个月花了多少 / 钱花哪了” |
| `mucage_export_ledger` | 导出手机可导入的同步文件，不改本机账本 | 用户说“导出给手机 / 备份账本” |
| `mucage_import_ledger` | 导入手机账本并合并，只合并不改源文件 | 用户说“把手机账本导进来 / 合并账本” |

另外提供一个**非 LLM** 的插件入口 `mucage_status`，用于在插件管理器里手动检查 `[mucage]` 配置和 MCP 连通性（返回 `Ok({...})`，含 `connected`、`pid`、`backend`、`toolCount`、`tools` 等字段）。

`mucage_list_records` 返回给模型的记录只保留 `id / date / amount / type / category / note`，避免把内部大对象全部塞进上下文；`limit` 默认 20、最大 100。

## 安装

1. 将整个 `sanbailiang/` 目录放入 N.E.K.O 源码的 `plugin/plugins/` 下（或在插件中心安装后找到它的安装目录）。
2. 编辑该目录中的 `plugin.toml`，把 `[mucage]` 段改成你自己的真实路径（见下一节）。
3. 启动 N.E.K.O，打开插件面板，刷新插件列表并启动「此地无银三百两账本」。
4. 在插件管理器执行 `mucage_status`，确认 `connected: true` 且能看到 13 个 MCP 工具。

> 插件启动时**不会**拉起 MCP 子进程；第一次调用工具（或第一次执行 `mucage_status`）时才懒启动。插件关闭/重载时会关闭 stdin、等待退出，超时后 `terminate` / `kill`，不会留下孤儿进程。

## 配置 `[mucage]`

### 方式一：Python 包装器（推荐）

```toml
[mucage]
mcp_command = "python"
mcp_args = ["C:\\path\\to\\MucageLedger\\mucage_mcp.py"]
request_timeout = 30
```

`mucage_mcp.py` 与 `MucageLedger.exe` 放在同一目录时，包装器会自动找到 exe；如果 exe 在别处，可设置环境变量 `MUCAGE_LEDGER_EXE`：

```powershell
$env:MUCAGE_LEDGER_EXE = "D:\Apps\MucageLedger\MucageLedger.exe"
```

### 方式二：直接启动 exe

```toml
[mucage]
mcp_command = "C:\\path\\to\\MucageLedger\\MucageLedger.exe"
mcp_args = ["--mcp"]
request_timeout = 30
```

TOML 字符串里的反斜杠必须写成 `\\`；也可以用正斜杠，Windows 同样支持：

```toml
mcp_args = ["C:/path/to/MucageLedger/mucage_mcp.py"]
```

### `request_timeout`

单次 MCP 请求超时，默认 30 秒，自动限制在 1–300 秒。账本很大、导出/导入较慢时可以调大；超过 300 会被截断为 300。

### 环境变量

- `MUCAGE_LEDGER_EXE`：包装器模式下指定 `MucageLedger.exe` 的完整路径。
- `MUCAGE_LEDGER_HOME`：指定另一个账本目录（目录下的 `ledger.json`）。

> **重要（实测行为）**：`MUCAGE_LEDGER_HOME` 只有在所指向的目录**已经存在 `ledger.json`** 时才生效。如果目录是空的，MucageLedger 会回退到真实的 `%APPDATA%\MucageLedger\ledger.json`。做隔离测试时务必先往临时目录放入一个合法的 `ledger.json`，否则可能改到真实账本。

## 使用前必读

1. **桌面窗口和 MCP 不能同时写账本。** 界面和 MCP 各自在内存里维护账本，同时运行会用其中一方覆盖另一方。让猫娘记账前，请先关闭 MucageLedger 桌面窗口；只想查询余额时也建议关闭，避免误操作。
2. **MCP 托盘图标。** MCP 服务启动后，Windows 通知区域会出现绿色 **¥** 托盘图标。鼠标悬停可以看到 PID 和账本路径；右键菜单可以「显示 MCP 状态」或「停止 MCP 服务」。插件异常退出后如果图标还在，可以用它停止服务，或在任务管理器结束带 `--mcp` 参数的 `MucageLedger.exe`。
3. **数据安全。** 这些工具拥有与界面相同的写权限。批量操作前建议先备份 `%APPDATA%\MucageLedger\ledger.json`。

## 导入 / 导出

- `mucage_export_ledger(path?)` **不会修改本机账本**；省略 `path` 时由 MCP 写入默认导出目录：
  - 默认：`%APPDATA%\MucageLedger\exports\SanBaiLiang-ledger-*.json`
  - 设置了 `MUCAGE_LEDGER_HOME` 时：`<MUCAGE_LEDGER_HOME>\exports\...`
- `mucage_import_ledger(path)` **只合并、不改源文件**：按 `syncId` 去重、`updatedAt` 较新者胜出。重复导入同一文件时不会重复插入，结果里会体现 `skipped`。
- 导出文件可以直接传到手机，在手机「设置 → 数据同步 → 从文件导入」中合并。

## 在猫娘计划里启用并验证

1. 确认账本软件已安装、能正常打开，并已关闭桌面窗口。
2. 启动 N.E.K.O，在插件面板启动本插件；日志里应出现 `sanbailiang 已加载`。
3. 执行 `mucage_status`，确认：
   - `configured: true`
   - `connected: true`
   - `toolCount: 13`
   - `backend` 为 `asyncio` 或 `thread`
4. 打开与猫娘的对话，依次尝试：
   - “我还有多少钱？”→ 模型应调用 `mucage_get_balance`
   - “这个月钱都花哪了？”→ `mucage_get_stats`
   - “帮我记一笔，午饭 35.5 元，分类吃”→ `mucage_list_categories`（可选）+ `mucage_add_record`
5. 如果模型看不到工具，先重载插件（见下一节）。

## 已知限制

- **main_server 重启后，`@llm_tool` 注册会丢失。** 这是官方文档说明的限制：`@llm_tool` 只在插件启动时向 main_server 注册一次。main_server 重启后模型将看不到这些工具，需要在插件管理器里**重载插件**恢复（或重启插件）。
- 只支持本机 stdio，不支持远程 MCP / SSE / HTTP。
- 需要本机已安装「此地无银三百两」桌面端（MCP 版本 ≥ 10.27.2）。
- Windows 路径在 TOML 中需要转义：`\\` 或改用 `/`。
- 插件只暴露上述 8 个对话工具；MCP 服务本身的 `update_record` / `delete_record` / 分类增删改、`import/export` 的其余能力仍可在其他 MCP 客户端中使用。
- 由于所有工具调用串行化，一次只能等一个 MCP 请求完成；默认 30 秒超时。
- 如果 N.E.K.O 宿主使用 `SelectorEventLoop`，Windows 上 `asyncio.create_subprocess_exec` 不可用。插件会自动改用 `subprocess.Popen` + 后台线程读 stdout 的兼容实现（`mucage_status` 的 `backend` 会显示 `thread`），行为与正常路径一致。

## 测试

测试用真实的 `MucageLedger.exe --mcp`，但每个用例都会创建临时 `MUCAGE_LEDGER_HOME` 并**预先写入一个隔离的 `ledger.json`**，绝不动真实的 `%APPDATA%\MucageLedger\ledger.json`。

```powershell
$env:MUCAGE_LEDGER_EXE = "C:\path\to\MucageLedger\MucageLedger.exe"

# 在插件根目录（sanbailiang/）运行
uv run --no-project --with pytest python -m pytest tests -q
```

测试覆盖：

- `initialize`、`tools/list`
- `set_balance 1000`
- `add_record 35.5`
- `list_records` / `get_stats`
- `export_ledger` 到临时文件
- 把导出文件 `import_ledger` 到另一个临时账本目录
- 重复导入应 `skipped`、文件不存在应报错
- 关闭后再次调用可重新启动、`SelectorEventLoop` 线程回退、超时清理
- 测试结束后用 PID 检查确认没有残留 MCP 子进程

插件就绪检查：

```bash
# 在 N.E.K.O 源码根目录
uv run neko-plugin check sanbailiang
```

## 仓库与隐私

本仓库只包含插件源码、配置示例、README 和测试。**不要提交**：

- `MucageLedger.exe`、`mucage_mcp.py`、`ledger.json`
- 导出文件、日志
- 任何令牌或本机私有路径（`plugin.toml` 里的路径请使用占位符或你自己的本地配置）

账本数据只在本地 stdio 通道中传递，插件不访问网络；是否把结果发给模型由 N.E.K.O 当前使用的模型决定。
