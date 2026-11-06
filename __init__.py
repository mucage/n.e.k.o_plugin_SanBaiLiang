"""MucageLedger plugin entry point.

All ledger reads and writes go through the local MucageLedger MCP service.
This plugin never opens ``ledger.json`` directly.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Mapping

from plugin.sdk.plugin import (
    NekoPluginBase,
    Ok,
    lifecycle,
    llm_tool,
    neko_plugin,
    plugin_entry,
)

from .mcp_client import (
    McpArgumentError,
    McpConfigError,
    McpError,
    McpStartupError,
    McpTimeoutError,
    McpToolError,
    MucageMcpClient,
    clamp_timeout,
)

_RECORD_FIELDS = ("id", "date", "amount", "type", "category", "note")
_TOOL_TIMEOUT = 300.0
_DEFAULT_TIMEOUT = 30.0


@neko_plugin
class MucageLedgerPlugin(NekoPluginBase):
    """让猫娘在对话中查询和操作本机「此地无银三百两」账本。"""

    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self.logger = ctx.logger
        self._client: MucageMcpClient | None = None
        self._client_lock: asyncio.Lock | None = None
        self._config: dict[str, Any] = {}
        self._config_error: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @lifecycle(id="startup")
    async def on_startup(self, **_):
        await self._reload_config()
        self.logger.info("mucage_ledger 已加载；MCP 子进程将在首次工具调用时启动")
        return Ok({"status": "ready", "configured": self._client is not None})

    @lifecycle(id="shutdown")
    async def on_shutdown(self, **_):
        await self._close_client()
        self.logger.info("mucage_ledger 已停止，MCP 子进程已清理")
        return Ok({"status": "stopped"})

    @lifecycle(id="reload")
    async def on_reload(self, **_):
        await self._reload_config()
        return Ok({"status": "reloaded", "configured": self._client is not None})

    # ------------------------------------------------------------------
    # Configuration / client lifecycle
    # ------------------------------------------------------------------
    def _ensure_client_lock(self) -> asyncio.Lock:
        if self._client_lock is None:
            self._client_lock = asyncio.Lock()
        return self._client_lock

    async def _reload_config(self) -> None:
        lock = self._ensure_client_lock()
        async with lock:
            await self._close_client_locked()
            try:
                cfg = await self.config.dump()
                section = cfg.get("mucage")
                if not isinstance(section, Mapping):
                    raise McpConfigError(
                        "plugin.toml 缺少 [mucage] 配置段，请先配置 mcp_command 与 mcp_args。"
                    )

                command = str(section.get("mcp_command") or "").strip()
                if not command:
                    raise McpConfigError(
                        "[mucage].mcp_command 不能为空，请填写 python 或 MucageLedger.exe 的路径。"
                    )

                raw_args = section.get("mcp_args")
                if raw_args is None:
                    args: list[str] = []
                elif isinstance(raw_args, str):
                    args = [raw_args]
                elif isinstance(raw_args, (list, tuple)):
                    args = [str(item) for item in raw_args]
                else:
                    raise McpConfigError("[mucage].mcp_args 必须是字符串数组。")

                timeout = clamp_timeout(section.get("request_timeout"), default=_DEFAULT_TIMEOUT)
                self._config = {
                    "command": command,
                    "args": args,
                    "request_timeout": timeout,
                }
                self._client = MucageMcpClient(
                    command,
                    args,
                    request_timeout=timeout,
                    logger=self.logger,
                )
                self._config_error = None
                self.logger.info(
                    f"mucage_ledger 配置已加载：command={command} "
                    f"args={len(args)} timeout={timeout:g}s"
                )
            except Exception as exc:
                self._config = {}
                self._config_error = (
                    exc.user_message if isinstance(exc, McpError) else str(exc)
                )
                self.logger.warning(f"mucage_ledger 配置无效：{self._config_error}")

    async def _close_client_locked(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            await client.close()
        except Exception as exc:  # pragma: no cover - defensive cleanup
            self.logger.warning(f"关闭 MCP 客户端时出错：{exc}")

    async def _close_client(self) -> None:
        lock = self._client_lock
        if lock is None:
            return
        async with lock:
            await self._close_client_locked()

    # ------------------------------------------------------------------
    # Tool-call plumbing
    # ------------------------------------------------------------------
    @staticmethod
    def _tool_error(code: str, message: str) -> dict[str, Any]:
        return {
            "output": {"ok": False, "error": message},
            "is_error": True,
            "error": code,
        }

    async def _call(self, tool: str, arguments: dict[str, Any]) -> Any:
        client = self._client
        if client is None:
            return self._tool_error(
                "MUCAGE_CONFIG",
                self._config_error or "插件尚未加载 [mucage] 配置，请检查 plugin.toml。",
            )
        try:
            return await client.call_tool(tool, arguments)
        except asyncio.CancelledError:
            raise
        except McpTimeoutError:
            return self._tool_error(
                "MUCAGE_TIMEOUT",
                f"MCP 服务响应超时（{client.request_timeout:g} 秒）。"
                "请确认账本软件没有卡住，或在 [mucage].request_timeout 中调大超时。",
            )
        except McpStartupError as exc:
            self.logger.warning(
                f"MCP 启动失败：{exc.user_message}（detail={exc.detail}）"
            )
            return self._tool_error(
                "MUCAGE_STARTUP",
                "无法启动「此地无银三百两」MCP 服务。请确认账本软件已安装，"
                "并检查 [mucage] 的 mcp_command / mcp_args；详细原因见插件日志。",
            )
        except McpConfigError as exc:
            return self._tool_error("MUCAGE_CONFIG", exc.user_message)
        except McpToolError as exc:
            return self._tool_error("MUCAGE_TOOL_ERROR", exc.user_message)
        except McpArgumentError as exc:
            return self._tool_error("MUCAGE_INVALID_ARGUMENT", exc.user_message)
        except McpError as exc:
            self.logger.warning(
                f"MCP 调用失败：{exc.user_message}（detail={exc.detail}）"
            )
            return self._tool_error(exc.code, exc.user_message)
        except Exception as exc:  # pragma: no cover - defensive boundary
            self.logger.exception(f"mucage_ledger 工具 {tool} 失败")
            return self._tool_error("MUCAGE_INTERNAL", f"插件内部错误：{exc}")

    @staticmethod
    def _as_amount(value: Any, *, positive: bool = False) -> float:
        if isinstance(value, bool) or value is None:
            raise McpArgumentError("金额必须是数字")
        try:
            amount = float(value)
        except (TypeError, ValueError) as exc:
            raise McpArgumentError("金额必须是数字") from exc
        if positive and amount <= 0:
            raise McpArgumentError("金额必须大于 0")
        return amount

    @staticmethod
    def _normalize_type(value: Any, *, default: str = "支出") -> str:
        text = default if value is None else str(value).strip()
        if not text:
            text = default
        upper = text.upper()
        if upper in ("支出", "EXPENSE"):
            return "支出"
        if upper in ("收入", "INCOME"):
            return "收入"
        raise McpArgumentError("type 只能是「支出」或「收入」")

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    # ------------------------------------------------------------------
    # LLM tools
    # ------------------------------------------------------------------
    @llm_tool(
        name="mucage_get_balance",
        description=(
            "查询本机「此地无银三百两」账本的当前总余额、余额锚点（锚点余额与时间）和账目数量。"
            "当用户问“我还有多少钱 / 余额多少 / 账本里还有多少钱”时调用。只读，不会修改账本。"
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        timeout=_TOOL_TIMEOUT,
    )
    async def mucage_get_balance(self, **_):
        return await self._call("get_balance", {})

    @llm_tool(
        name="mucage_set_balance",
        description=(
            "把本机账本的总余额直接设置为指定金额（单位：元），会以今天 0 点为新锚点。"
            "当用户明确说“把余额改成 / 设为 X 元”时调用。只是查询余额请用 mucage_get_balance，不要调用本工具。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "amount": {
                    "type": "number",
                    "description": "新的总余额，单位元，可为小数",
                }
            },
            "required": ["amount"],
        },
        timeout=_TOOL_TIMEOUT,
    )
    async def mucage_set_balance(self, *, amount: float, **_):
        try:
            value = self._as_amount(amount)
        except McpArgumentError as exc:
            return self._tool_error(exc.code, exc.user_message)
        return await self._call("set_balance", {"amount": value})

    @llm_tool(
        name="mucage_list_categories",
        description=(
            "列出本机账本可用的支出/收入分类名称与 id。记账前不确定分类名时先调用；"
            "用户问“有哪些分类 / 支出分类 / 收入分类”时也调用。只读。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["支出", "收入"],
                    "description": "可选：只看支出分类或收入分类；省略则返回全部分类",
                }
            },
            "required": [],
        },
        timeout=_TOOL_TIMEOUT,
    )
    async def mucage_list_categories(self, *, type: str | None = None, **_):
        arguments: dict[str, Any] = {}
        text = self._optional_text(type)
        if text:
            arguments["type"] = text
        return await self._call("list_categories", arguments)

    @llm_tool(
        name="mucage_list_records",
        description=(
            "查询账目明细，可按日期区间、收支类型、分类、备注关键词过滤。"
            "用户问“最近记了什么 / 查一下某天的账 / 找某笔账 / 上个月有哪些支出”时调用。"
            "返回记录只包含 id、日期、金额、类型、分类、备注；limit 默认 20、最大 100。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "start": {
                    "type": "string",
                    "description": "开始日期或时间，如 2026-11-01 或 2026-11-01 08:00",
                },
                "end": {
                    "type": "string",
                    "description": "结束日期或时间；只给日期时包含当天",
                },
                "type": {
                    "type": "string",
                    "enum": ["支出", "收入"],
                    "description": "可选：只看支出或收入",
                },
                "category": {
                    "type": "string",
                    "description": "分类名称或分类 id",
                },
                "keyword": {
                    "type": "string",
                    "description": "备注关键词",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                    "description": "最多返回多少条，默认 20，最大 100",
                },
            },
            "required": [],
        },
        timeout=_TOOL_TIMEOUT,
    )
    async def mucage_list_records(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        type: str | None = None,
        category: str | None = None,
        keyword: str | None = None,
        limit: int = 20,
        **_,
    ):
        if isinstance(limit, bool) or limit is None:
            limit_value = 20
        else:
            try:
                limit_value = int(limit)
            except (TypeError, ValueError):
                return self._tool_error("MUCAGE_INVALID_ARGUMENT", "limit 必须是整数")
            limit_value = max(1, min(limit_value, 100))

        arguments: dict[str, Any] = {"limit": limit_value}
        for key, value in (
            ("start", start),
            ("end", end),
            ("category", category),
            ("keyword", keyword),
        ):
            text = self._optional_text(value)
            if text:
                arguments[key] = text
        if type is not None and str(type).strip():
            try:
                arguments["type"] = self._normalize_type(type)
            except McpArgumentError as exc:
                return self._tool_error(exc.code, exc.user_message)

        result = await self._call("list_records", arguments)
        if isinstance(result, dict) and isinstance(result.get("records"), list):
            trimmed = []
            for record in result["records"]:
                if isinstance(record, Mapping):
                    trimmed.append(
                        {
                            field: record[field]
                            for field in _RECORD_FIELDS
                            if field in record
                        }
                    )
            result = {**result, "records": trimmed}
        return result

    @llm_tool(
        name="mucage_add_record",
        description=(
            "在本机账本记一笔支出或收入。用户说“记一笔 / 花了 X 元 / 午饭 X 元 / 收到工资 X 元”时调用。"
            "金额单位元；不确定分类名时先调用 mucage_list_categories。日期可省略，省略表示当前时间。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "amount": {
                    "type": "number",
                    "minimum": 0.01,
                    "description": "金额，单位元，必须大于 0",
                },
                "type": {
                    "type": "string",
                    "enum": ["支出", "收入"],
                    "default": "支出",
                    "description": "支出或收入，默认支出",
                },
                "category": {
                    "type": "string",
                    "description": "分类名称或分类 id；省略归入同类型「其他」",
                },
                "note": {
                    "type": "string",
                    "description": "备注，例如“午饭”",
                },
                "date": {
                    "type": "string",
                    "description": "记账时间，支持 YYYY-MM-DD 或 YYYY-MM-DD HH:mm；省略为当前时间",
                },
                "limitExempt": {
                    "type": "integer",
                    "enum": [0, 1, 2],
                    "default": 0,
                    "description": "限额豁免：0 不豁免，1 仅豁免日限额，2 全部豁免",
                },
            },
            "required": ["amount"],
        },
        timeout=_TOOL_TIMEOUT,
    )
    async def mucage_add_record(
        self,
        *,
        amount: float,
        type: str = "支出",
        category: str | None = None,
        note: str | None = None,
        date: str | None = None,
        limitExempt: int | None = None,
        **_,
    ):
        try:
            amount_value = self._as_amount(amount, positive=True)
            type_value = self._normalize_type(type)
        except McpArgumentError as exc:
            return self._tool_error(exc.code, exc.user_message)

        arguments: dict[str, Any] = {"amount": amount_value, "type": type_value}
        for key, value in (("category", category), ("note", note), ("date", date)):
            text = self._optional_text(value)
            if text:
                arguments[key] = text

        if limitExempt is not None:
            if isinstance(limitExempt, bool):
                return self._tool_error("MUCAGE_INVALID_ARGUMENT", "limitExempt 只能是 0、1 或 2")
            try:
                exempt = int(limitExempt)
            except (TypeError, ValueError):
                return self._tool_error("MUCAGE_INVALID_ARGUMENT", "limitExempt 只能是 0、1 或 2")
            if exempt not in (0, 1, 2):
                return self._tool_error("MUCAGE_INVALID_ARGUMENT", "limitExempt 只能是 0、1 或 2")
            arguments["limitExempt"] = exempt

        return await self._call("add_record", arguments)

    @llm_tool(
        name="mucage_get_stats",
        description=(
            "读取账本统计：指定月份（默认本月）或自定义区间的支出/收入合计、分类占比、每日收支序列、前几大支出。"
            "用户问“这个月花了多少 / 钱都花哪了 / 看看统计 / 哪几笔支出最大”时调用。只读。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "month": {
                    "type": "string",
                    "description": "月份，格式 YYYY-MM，例如 2026-11；默认本月",
                },
                "start": {
                    "type": "string",
                    "description": "自定义开始日期，与 end 搭配使用",
                },
                "end": {
                    "type": "string",
                    "description": "自定义结束日期",
                },
            },
            "required": [],
        },
        timeout=_TOOL_TIMEOUT,
    )
    async def mucage_get_stats(
        self,
        *,
        month: str | None = None,
        start: str | None = None,
        end: str | None = None,
        **_,
    ):
        arguments: dict[str, Any] = {}
        for key, value in (("month", month), ("start", start), ("end", end)):
            text = self._optional_text(value)
            if text:
                arguments[key] = text
        return await self._call("get_stats", arguments)

    @llm_tool(
        name="mucage_export_ledger",
        description=(
            "把本机账本导出为手机可导入的同步文件；本机账本不会被修改。"
            "用户说“导出账本给手机 / 备份账本 / 生成同步文件”时调用。"
            "省略 path 时由 MCP 写入默认 exports 目录（%APPDATA%\\MucageLedger\\exports）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "导出文件的完整路径，例如 D:\\sync\\账本.json；省略则自动命名",
                }
            },
            "required": [],
        },
        timeout=_TOOL_TIMEOUT,
    )
    async def mucage_export_ledger(self, *, path: str | None = None, **_):
        arguments: dict[str, Any] = {}
        text = self._optional_text(path)
        if text:
            arguments["path"] = text
        return await self._call("export_ledger", arguments)

    @llm_tool(
        name="mucage_import_ledger",
        description=(
            "导入手机或另一台电脑导出的账本文件，并与本机账本合并；只合并、不会修改源文件。"
            "用户说“把手机账本导进来 / 同步账本 / 合并账本文件”时调用。需要提供导出文件的完整路径。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要导入的账本文件完整路径，例如 D:\\sync\\手机账本.json",
                }
            },
            "required": ["path"],
        },
        timeout=_TOOL_TIMEOUT,
    )
    async def mucage_import_ledger(self, *, path: str, **_):
        text = self._optional_text(path)
        if not text:
            return self._tool_error("MUCAGE_INVALID_ARGUMENT", "path 不能为空")
        result = await self._call("import_ledger", {"path": text})
        if isinstance(result, dict) and "is_error" not in result:
            result.setdefault("hint", "导入只合并到本机账本，不会修改源文件。")
        return result

    # ------------------------------------------------------------------
    # Manual status entry (plugin manager, not an LLM tool)
    # ------------------------------------------------------------------
    @plugin_entry(
        id="mucage_status",
        name="账本 MCP 状态",
        description="检查 [mucage] 配置与 MCP 服务连通性；不会写入账本。",
    )
    async def mucage_status(self, **_):
        info: dict[str, Any] = {
            "configured": self._client is not None,
            "command": self._config.get("command"),
            "args": list(self._config.get("args") or []),
            "requestTimeout": self._config.get("request_timeout", _DEFAULT_TIMEOUT),
            "ledgerHome": os.environ.get("MUCAGE_LEDGER_HOME") or r"%APPDATA%\MucageLedger",
            "connected": False,
            "note": "桌面窗口和 MCP 不能同时写账本；MCP 启动后托盘会出现绿色 ¥ 图标，右键可停止服务。",
        }
        if self._config_error:
            info["error"] = self._config_error
        if self._client is None:
            info.setdefault("error", "MCP 客户端未初始化，请检查 [mucage] 配置。")
            return Ok(info)

        try:
            tools = await self._client.list_tools()
        except McpError as exc:
            info["error"] = exc.user_message
            info["lastError"] = self._client.last_error
            info["stderrTail"] = self._client.stderr_tail[-5:]
            return Ok(info)

        info.update(
            {
                "connected": True,
                "initialized": self._client.initialized,
                "backend": self._client.backend,
                "pid": self._client.last_pid,
                "toolCount": len(tools),
                "tools": [str(tool.get("name")) for tool in tools if tool.get("name")],
                "server": self._client.server_info,
            }
        )
        return Ok(info)
