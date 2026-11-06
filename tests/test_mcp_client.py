"""Integration tests for the MucageLedger MCP stdio client.

These tests drive the real ``MucageLedger.exe --mcp`` child process with an
isolated ``MUCAGE_LEDGER_HOME`` directory. They never touch the real
``%APPDATA%\\MucageLedger\\ledger.json``.

Important: MucageLedger only honours ``MUCAGE_LEDGER_HOME`` when that
directory already contains a ``ledger.json``. Every test seeds one before
starting the client; otherwise the app falls back to the real AppData ledger.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from mcp_client import (  # noqa: E402
    McpStartupError,
    McpTimeoutError,
    McpToolError,
    MucageMcpClient,
)

TOOL_NAMES = {
    "get_balance",
    "set_balance",
    "list_records",
    "add_record",
    "update_record",
    "delete_record",
    "list_categories",
    "add_category",
    "update_category",
    "delete_category",
    "get_stats",
    "import_ledger",
    "export_ledger",
}

DEFAULT_CATEGORIES = [
    {"id": 101, "name": "吃", "type": "EXPENSE", "icon": "restaurant", "sortOrder": 0, "updatedAt": 0, "deleted": False},
    {"id": 102, "name": "用", "type": "EXPENSE", "icon": "shopping", "sortOrder": 1, "updatedAt": 0, "deleted": False},
    {"id": 103, "name": "玩", "type": "EXPENSE", "icon": "entertainment", "sortOrder": 2, "updatedAt": 0, "deleted": False},
    {"id": 104, "name": "学", "type": "EXPENSE", "icon": "education", "sortOrder": 3, "updatedAt": 0, "deleted": False},
    {"id": 105, "name": "出行", "type": "EXPENSE", "icon": "transport", "sortOrder": 4, "updatedAt": 0, "deleted": False},
    {"id": 116, "name": "其他", "type": "EXPENSE", "icon": "more", "sortOrder": 5, "updatedAt": 0, "deleted": False},
    {"id": 201, "name": "工资", "type": "INCOME", "icon": "salary", "sortOrder": 0, "updatedAt": 0, "deleted": False},
    {"id": 202, "name": "奖金", "type": "INCOME", "icon": "bonus", "sortOrder": 1, "updatedAt": 0, "deleted": False},
    {"id": 203, "name": "理财", "type": "INCOME", "icon": "finance", "sortOrder": 2, "updatedAt": 0, "deleted": False},
    {"id": 204, "name": "兼职", "type": "INCOME", "icon": "parttime", "sortOrder": 3, "updatedAt": 0, "deleted": False},
    {"id": 205, "name": "红包", "type": "INCOME", "icon": "redpacket", "sortOrder": 4, "updatedAt": 0, "deleted": False},
    {"id": 206, "name": "其他", "type": "INCOME", "icon": "more", "sortOrder": 5, "updatedAt": 0, "deleted": False},
]


def _exe_path() -> str:
    raw = os.environ.get("MUCAGE_LEDGER_EXE", "").strip()
    if not raw:
        pytest.skip("设置 MUCAGE_LEDGER_EXE 指向 MucageLedger.exe 后运行集成测试")
    path = Path(raw)
    if not path.is_file():
        pytest.skip(f"MUCAGE_LEDGER_EXE 不存在：{path}")
    return str(path)


def _real_ledger_state() -> tuple[int, int] | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    path = Path(appdata) / "MucageLedger" / "ledger.json"
    if not path.is_file():
        return None
    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns)


@pytest.fixture(autouse=True, scope="module")
def _guard_real_ledger():
    before = _real_ledger_state()
    yield
    after = _real_ledger_state()
    assert before == after, (
        "集成测试意外修改了真实账本 %APPDATA%\\MucageLedger\\ledger.json；"
        "请检查 MUCAGE_LEDGER_HOME 是否已包含 ledger.json。"
    )


def _seed_ledger(home: Path, *, balance_cents: int = 888888) -> None:
    """Create an isolated ledger so MucageLedger honours MUCAGE_LEDGER_HOME."""
    home.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": "mucage-ledger",
        "version": 2,
        "exportedAt": 0,
        "balance": {
            "balanceCents": balance_cents,
            "anchoredAt": 1793548800000,
            "initialized": True,
            "updatedAt": 0,
        },
        "categories": DEFAULT_CATEGORIES,
        "records": [],
    }
    (home / "ledger.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _client(home: Path, exe: str, *, timeout: float = 30.0) -> MucageMcpClient:
    assert (home / "ledger.json").is_file(), (
        "启动 MCP 前必须在 MUCAGE_LEDGER_HOME 中放入 ledger.json，"
        "否则 MucageLedger 会回退到真实 AppData 账本。"
    )
    return MucageMcpClient(
        exe,
        ["--mcp"],
        request_timeout=timeout,
        env={"MUCAGE_LEDGER_HOME": str(home)},
    )


def _pid_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    kernel32 = ctypes.windll.kernel32
    process_query_limited_information = 0x1000
    still_active = 259
    handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def test_mcp_full_flow() -> None:
    exe = _exe_path()
    with tempfile.TemporaryDirectory(prefix="mucage-test-") as tmp:
        root = Path(tmp)
        home_a = root / "home_a"
        home_b = root / "home_b"
        _seed_ledger(home_a)
        _seed_ledger(home_b, balance_cents=999999)
        export_path = root / "export.json"
        missing_path = root / "missing.json"

        async def scenario() -> None:
            client = _client(home_a, exe)
            other = None
            try:
                tools = await client.list_tools()
                names = {str(item.get("name")) for item in tools}
                assert TOOL_NAMES <= names
                assert client.initialized
                assert client.server_info.get("name") == "mucage-ledger"

                balance = await client.call_tool("get_balance", {})
                assert balance["balance"] == pytest.approx(8888.88)

                changed = await client.call_tool("set_balance", {"amount": 1000})
                assert changed["balance"] == pytest.approx(1000.0)

                added = await client.call_tool(
                    "add_record",
                    {
                        "amount": 35.5,
                        "type": "支出",
                        "category": "吃",
                        "note": "测试午饭",
                        "date": "2026-11-06 12:00",
                    },
                )
                assert added["record"]["amount"] == pytest.approx(35.5)
                assert added["record"]["category"] == "吃"

                records = await client.call_tool(
                    "list_records",
                    {"keyword": "测试午饭", "limit": 20},
                )
                assert records["count"] == 1
                assert records["records"][0]["id"] == added["record"]["id"]

                stats = await client.call_tool("get_stats", {"month": "2026-11"})
                assert stats["expense"]["amount"] == pytest.approx(35.5)
                assert stats["expense"]["count"] == 1

                exported = await client.call_tool(
                    "export_ledger",
                    {"path": str(export_path)},
                )
                assert Path(exported["path"]).is_file()
                exported_payload = json.loads(export_path.read_text(encoding="utf-8"))
                assert exported_payload["format"] == "mucage-ledger"

                other = _client(home_b, exe)
                imported = await other.call_tool(
                    "import_ledger",
                    {"path": str(export_path)},
                )
                assert imported.get("inserted", 0) >= 1

                duplicate = await other.call_tool(
                    "import_ledger",
                    {"path": str(export_path)},
                )
                assert duplicate.get("inserted", 0) == 0
                assert duplicate.get("skipped", 0) >= 1

                with pytest.raises(McpToolError):
                    await other.call_tool(
                        "import_ledger",
                        {"path": str(missing_path)},
                    )
            finally:
                await client.close()
                if other is not None:
                    await other.close()

        asyncio.run(scenario())


def test_restart_after_close_and_no_orphan() -> None:
    exe = _exe_path()
    with tempfile.TemporaryDirectory(prefix="mucage-restart-") as tmp:
        home = Path(tmp) / "home"
        _seed_ledger(home)
        pids: list[int] = []

        async def scenario() -> None:
            client = _client(home, exe)
            try:
                await client.list_tools()
                first_pid = client.last_pid
                assert first_pid is not None
                assert _pid_alive(first_pid)
                pids.append(first_pid)

                await client.close()
                assert not _pid_alive(first_pid)

                await client.list_tools()
                second_pid = client.last_pid
                assert second_pid is not None
                assert second_pid != first_pid
                pids.append(second_pid)
            finally:
                await client.close()

        asyncio.run(scenario())
        assert pids
        for pid in pids:
            assert not _pid_alive(pid), f"残留 MCP 子进程 PID={pid}"


@pytest.mark.skipif(os.name != "nt", reason="SelectorEventLoop fallback is Windows-specific")
def test_selector_event_loop_thread_fallback() -> None:
    exe = _exe_path()
    with tempfile.TemporaryDirectory(prefix="mucage-selector-") as tmp:
        home = Path(tmp) / "home"
        _seed_ledger(home)
        pids: list[int] = []

        async def scenario() -> None:
            client = _client(home, exe)
            try:
                tools = await client.list_tools()
                assert len(tools) >= len(TOOL_NAMES)
                assert client.backend == "thread"
                balance = await client.call_tool("get_balance", {})
                assert balance["balance"] == pytest.approx(8888.88)
                assert client.last_pid is not None
                pids.append(client.last_pid)
            finally:
                await client.close()

        loop = asyncio.SelectorEventLoop()
        try:
            loop.run_until_complete(scenario())
        finally:
            loop.close()
        for pid in pids:
            assert not _pid_alive(pid), f"残留 MCP 子进程 PID={pid}"


def test_missing_command_reports_startup_error() -> None:
    with tempfile.TemporaryDirectory(prefix="mucage-missing-") as tmp:
        home = Path(tmp) / "home"
        _seed_ledger(home)
        missing_exe = Path(tmp) / "does-not-exist.exe"

        async def scenario() -> None:
            client = MucageMcpClient(
                str(missing_exe),
                ["--mcp"],
                request_timeout=10,
                env={"MUCAGE_LEDGER_HOME": str(home)},
            )
            try:
                with pytest.raises(McpStartupError):
                    await client.list_tools()
            finally:
                await client.close()

        asyncio.run(scenario())


def test_request_timeout_kills_child() -> None:
    async def scenario() -> None:
        client = MucageMcpClient(
            sys.executable,
            ["-c", "import time; time.sleep(30)"],
            request_timeout=1,
        )
        pid = None
        try:
            with pytest.raises(McpTimeoutError):
                await client.list_tools()
            pid = client.last_pid
        finally:
            await client.close()
        if pid is not None:
            assert not _pid_alive(pid), f"超时后残留 MCP 子进程 PID={pid}"

    asyncio.run(scenario())
