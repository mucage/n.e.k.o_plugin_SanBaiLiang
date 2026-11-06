"""MCP stdio client for the local MucageLedger service.

Only ``initialize``, ``tools/list`` and ``tools/call`` are implemented. The
client is deliberately dependency-free (Python standard library only) because
it runs inside a plugin process that may not ship third-party packages.

Transport notes
---------------
* Normal path: ``asyncio.create_subprocess_exec`` with pipes.
* Windows compatibility path: if the host runs on ``SelectorEventLoop``
  where ``create_subprocess_exec`` is unavailable, the client falls back to
  ``subprocess.Popen`` plus a background thread that pumps stdout into a
  queue. Both transports speak UTF-8 JSON per line.

The client keeps at most one child process and serialises every request with
an ``asyncio.Lock``. Request ids are monotonically increasing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Any, Mapping, Sequence

DEFAULT_REQUEST_TIMEOUT = 30.0
MAX_REQUEST_TIMEOUT = 300.0
PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "neko-mucage", "version": "1.0"}

_EOF = object()


class McpError(Exception):
    """Base class for all MCP client failures surfaced to the plugin."""

    code = "MUCAGE_MCP_ERROR"

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.user_message = message
        self.detail = detail


class McpConfigError(McpError):
    code = "MUCAGE_CONFIG"


class McpArgumentError(McpError):
    code = "MUCAGE_INVALID_ARGUMENT"


class McpStartupError(McpError):
    code = "MUCAGE_STARTUP"


class McpTimeoutError(McpError):
    code = "MUCAGE_TIMEOUT"


class McpProtocolError(McpError):
    code = "MUCAGE_PROTOCOL"


class McpProcessExitError(McpError):
    code = "MUCAGE_PROCESS_EXIT"


class McpRpcError(McpError):
    code = "MUCAGE_RPC_ERROR"


class McpToolError(McpError):
    code = "MUCAGE_TOOL_ERROR"


def clamp_timeout(value: Any, *, default: float = DEFAULT_REQUEST_TIMEOUT) -> float:
    """Return a sane request timeout, capped at 300 seconds."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    if parsed <= 0:
        parsed = float(default)
    return max(1.0, min(parsed, MAX_REQUEST_TIMEOUT))


class MucageMcpClient:
    """One lazily-started MCP stdio connection to MucageLedger."""

    def __init__(
        self,
        command: str,
        args: Sequence[str] | None = None,
        *,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        env: Mapping[str, str] | None = None,
        cwd: str | Path | None = None,
        logger: Any = None,
    ) -> None:
        if not isinstance(command, str) or not command.strip():
            raise McpConfigError("没有配置 MCP 启动命令（mcp_command）")
        self._command = command.strip()
        self._args = [str(item) for item in (args or [])]
        self.request_timeout = clamp_timeout(request_timeout)
        self._env = {str(key): str(value) for key, value in (env or {}).items()}
        self._cwd = str(cwd) if cwd else None
        self._logger = logger

        self._lock = asyncio.Lock()

        self._proc: Any = None
        self._backend: str | None = None
        self._stderr_task: asyncio.Task[Any] | None = None
        self._stderr_thread: threading.Thread | None = None
        self._reader_thread: threading.Thread | None = None
        self._queue: queue.Queue[Any] | None = None
        self._thread_eof = False

        self._request_id = 0
        self._initialized = False
        self._server_info: dict[str, Any] = {}
        self._stderr_lines: deque[str] = deque(maxlen=40)
        self.last_error: str | None = None
        self.last_pid: int | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def __repr__(self) -> str:
        return f"MucageMcpClient(command={self._command!r}, args={len(self._args)})"

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def backend(self) -> str | None:
        return self._backend

    @property
    def server_info(self) -> dict[str, Any]:
        return dict(self._server_info)

    @property
    def stderr_tail(self) -> list[str]:
        return list(self._stderr_lines)

    @property
    def is_running(self) -> bool:
        return self._process_is_alive()

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Call one MCP tool and return its decoded JSON payload."""
        if not isinstance(name, str) or not name.strip():
            raise McpArgumentError("MCP 工具名不能为空")
        if arguments is not None and not isinstance(arguments, Mapping):
            raise McpArgumentError("MCP 工具参数必须是对象")

        payload = {"name": name.strip(), "arguments": dict(arguments or {})}
        async with self._lock:
            await self._ensure_started()
            result = await self._request("tools/call", payload, timeout=timeout)
            return self._decode_tool_result(result)

    async def list_tools(self, *, timeout: float | None = None) -> list[dict[str, Any]]:
        """Return the server's tool descriptors (used by the status entry)."""
        async with self._lock:
            await self._ensure_started()
            result = await self._request("tools/list", {}, timeout=timeout)
            tools = result.get("tools") if isinstance(result, dict) else None
            if not isinstance(tools, list):
                raise McpProtocolError("tools/list 没有返回 tools 数组")
            return [dict(item) for item in tools if isinstance(item, Mapping)]

    async def close(self) -> None:
        """Stop the child process and make the client reusable."""
        acquired = False
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=5.0)
            acquired = True
        except asyncio.TimeoutError:
            # A call is still in flight; still tear the process down so the
            # plugin never leaves an orphan behind.
            pass
        try:
            await self._shutdown_transport(force=True)
        finally:
            if acquired:
                self._lock.release()

    async def __aenter__(self) -> "MucageMcpClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Startup / initialization
    # ------------------------------------------------------------------
    async def _ensure_started(self) -> None:
        if self._process_is_alive():
            return
        if self._proc is not None:
            await self._shutdown_transport(force=True)
        self._reset_protocol_state()
        try:
            loop = asyncio.get_running_loop()
            use_thread = os.name == "nt" and isinstance(loop, asyncio.SelectorEventLoop)
            if use_thread:
                await self._start_thread()
            else:
                try:
                    await self._start_async()
                except NotImplementedError:
                    await self._shutdown_transport(force=True)
                    await self._start_thread()
            await self._initialize()
        except asyncio.CancelledError:
            await self._shutdown_transport(force=True)
            raise
        except McpError:
            await self._shutdown_transport(force=True)
            raise
        except Exception as exc:  # pragma: no cover - defensive boundary
            await self._shutdown_transport(force=True)
            raise McpStartupError(
                "启动「此地无银三百两」MCP 服务失败。请检查 [mucage] 的 "
                "mcp_command / mcp_args 是否指向可执行文件或脚本。",
                detail=str(exc),
            ) from exc

    async def _initialize(self) -> None:
        result = await self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": dict(CLIENT_INFO),
            },
            timeout=self.request_timeout,
        )
        if not isinstance(result, dict):
            raise McpProtocolError("initialize 返回格式异常")
        server_info = result.get("serverInfo")
        self._server_info = dict(server_info) if isinstance(server_info, Mapping) else {}
        await self._send_notification("notifications/initialized")
        self._initialized = True

    async def _start_async(self) -> None:
        env = self._build_env()
        kwargs: dict[str, Any] = {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "env": env,
        }
        if self._cwd:
            kwargs["cwd"] = self._cwd
        flags = self._creation_flags()
        if flags:
            kwargs["creationflags"] = flags
        self._log(f"启动 MCP（asyncio）：{self._command} {' '.join(self._args)}")
        try:
            self._proc = await asyncio.create_subprocess_exec(self._command, *self._args, **kwargs)
        except NotImplementedError:
            raise
        except FileNotFoundError as exc:
            raise McpStartupError(
                f"找不到 MCP 启动命令：{self._command}。请检查 [mucage].mcp_command。",
                detail=str(exc),
            ) from exc
        except OSError as exc:
            raise McpStartupError(
                f"无法启动 MCP 命令：{self._command}。请检查 [mucage].mcp_command。",
                detail=str(exc),
            ) from exc
        self._backend = "asyncio"
        self.last_pid = self._proc.pid
        self._stderr_task = asyncio.create_task(self._stderr_loop_async())

    async def _start_thread(self) -> None:
        env = self._build_env()
        flags = self._creation_flags()
        self._log(f"启动 MCP（thread fallback）：{self._command} {' '.join(self._args)}")
        try:
            self._proc = subprocess.Popen(
                [self._command, *self._args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=self._cwd,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=flags,
            )
        except FileNotFoundError as exc:
            raise McpStartupError(
                f"找不到 MCP 启动命令：{self._command}。请检查 [mucage].mcp_command。",
                detail=str(exc),
            ) from exc
        except OSError as exc:
            raise McpStartupError(
                f"无法启动 MCP 命令：{self._command}。请检查 [mucage].mcp_command。",
                detail=str(exc),
            ) from exc
        self._backend = "thread"
        self.last_pid = self._proc.pid
        self._queue = queue.Queue()
        self._thread_eof = False
        self._reader_thread = threading.Thread(
            target=self._stdout_thread,
            args=(self._proc, self._queue),
            name="mucage-mcp-stdout",
            daemon=True,
        )
        self._reader_thread.start()
        self._stderr_thread = threading.Thread(
            target=self._stderr_thread_main,
            args=(self._proc,),
            name="mucage-mcp-stderr",
            daemon=True,
        )
        self._stderr_thread.start()

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self._env)
        return env

    @staticmethod
    def _creation_flags() -> int:
        if os.name == "nt":
            return int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        return 0

    def _reset_protocol_state(self) -> None:
        self._request_id = 0
        self._initialized = False
        self._server_info = {}

    def _process_is_alive(self) -> bool:
        proc = self._proc
        if proc is None:
            return False
        if self._backend == "asyncio":
            return proc.returncode is None
        try:
            return proc.poll() is None
        except Exception:
            return False

    # ------------------------------------------------------------------
    # JSON-RPC transport
    # ------------------------------------------------------------------
    async def _request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if self._proc is None:
            raise McpProcessExitError("MCP 进程未启动")
        self._request_id += 1
        request_id = self._request_id
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": dict(params or {}),
        }
        try:
            await self._write_line(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
        except (BrokenPipeError, ConnectionResetError, OSError, RuntimeError) as exc:
            self.last_error = "MCP 进程已退出，无法写入请求"
            await self._shutdown_transport(force=True)
            raise McpProcessExitError(self.last_error, detail=str(exc)) from exc

        effective_timeout = clamp_timeout(timeout, default=self.request_timeout)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + effective_timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                self.last_error = f"MCP 请求 {method} 超时（{effective_timeout:g} 秒）"
                await self._shutdown_transport(force=True)
                raise McpTimeoutError(self.last_error)
            try:
                line = await self._read_line(remaining)
            except asyncio.TimeoutError:
                self.last_error = f"MCP 请求 {method} 超时（{effective_timeout:g} 秒）"
                await self._shutdown_transport(force=True)
                raise McpTimeoutError(self.last_error) from None
            if line is None:
                self.last_error = "MCP 进程已退出"
                await self._shutdown_transport(force=True)
                raise McpProcessExitError(
                    "「此地无银三百两」MCP 进程已退出，请确认账本软件仍可用后重试。"
                )
            line = line.lstrip("\ufeff").strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise McpProtocolError(
                    f"MCP 返回了非 JSON 内容：{line[:120]}",
                    detail=str(exc),
                ) from exc
            if not isinstance(payload, dict):
                raise McpProtocolError("MCP 返回值不是 JSON 对象")
            if payload.get("id") != request_id:
                # Notifications or a late response from a timed-out request.
                self._log(f"忽略 MCP 消息 id={payload.get('id')!r}")
                continue
            if "error" in payload:
                error = payload.get("error")
                if isinstance(error, Mapping):
                    message_text = str(error.get("message") or error)
                    code = error.get("code")
                else:
                    message_text = str(error)
                    code = None
                raise McpRpcError(
                    f"MCP 返回错误：{message_text}",
                    detail=f"code={code}",
                )
            result = payload.get("result")
            if result is None:
                return {}
            if not isinstance(result, dict):
                raise McpProtocolError("MCP result 不是 JSON 对象")
            return result

    async def _send_notification(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = dict(params)
        await self._write_line(json.dumps(message, ensure_ascii=False, separators=(",", ":")))

    async def _write_line(self, line: str) -> None:
        proc = self._proc
        if proc is None:
            raise McpProcessExitError("MCP 进程未启动")
        text = line.rstrip("\r\n") + "\n"
        if self._backend == "asyncio":
            if proc.stdin is None:
                raise McpProcessExitError("MCP stdin 不可用")
            proc.stdin.write(text.encode("utf-8"))
            await proc.stdin.drain()
            return
        if proc.stdin is None:
            raise McpProcessExitError("MCP stdin 不可用")
        await asyncio.to_thread(self._write_line_thread, proc, text)

    @staticmethod
    def _write_line_thread(proc: Any, text: str) -> None:
        proc.stdin.write(text)
        proc.stdin.flush()

    async def _read_line(self, timeout: float) -> str | None:
        if self._backend == "asyncio":
            proc = self._proc
            if proc is None or proc.stdout is None:
                return None
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
            if not raw:
                return None
            return raw.decode("utf-8", "replace")

        if self._queue is None:
            return None
        if self._thread_eof:
            return None
        try:
            item = await asyncio.to_thread(self._queue.get, True, timeout)
        except queue.Empty:
            raise asyncio.TimeoutError from None
        if item is _EOF:
            self._thread_eof = True
            return None
        return str(item)

    # ------------------------------------------------------------------
    # Result decoding
    # ------------------------------------------------------------------
    def _decode_tool_result(self, result: Mapping[str, Any]) -> Any:
        content = result.get("content")
        text: str | None = None
        if isinstance(content, list):
            for item in content:
                if isinstance(item, Mapping) and item.get("type") == "text":
                    raw = item.get("text")
                    if raw is not None:
                        text = str(raw)
                        break
        if text is None:
            raise McpProtocolError("tools/call 结果中没有文本内容")

        is_error = bool(result.get("isError"))
        if is_error:
            raise McpToolError(_humanize_tool_error(text))

        stripped = text.strip()
        if not stripped:
            raise McpProtocolError("tools/call 返回了空文本")
        try:
            return json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise McpProtocolError(
                f"MCP 工具返回的文本不是 JSON：{stripped[:120]}",
                detail=str(exc),
            ) from exc

    # ------------------------------------------------------------------
    # stderr / logging
    # ------------------------------------------------------------------
    def _log(self, message: str) -> None:
        if self._logger is None:
            return
        try:
            self._logger.debug(message)
        except Exception:
            pass

    def _record_stderr(self, line: str) -> None:
        text = str(line).strip()
        if not text:
            return
        self._stderr_lines.append(text)
        self._log(f"MCP stderr: {text}")

    async def _stderr_loop_async(self) -> None:
        try:
            proc = self._proc
            if proc is None or proc.stderr is None:
                return
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                self._record_stderr(line.decode("utf-8", "replace"))
        except asyncio.CancelledError:
            return
        except Exception as exc:  # pragma: no cover - defensive
            self._log(f"读取 MCP stderr 失败：{exc}")

    def _stderr_thread_main(self, proc: Any) -> None:
        try:
            if proc.stderr is None:
                return
            for line in proc.stderr:
                self._record_stderr(line)
        except Exception as exc:  # pragma: no cover - defensive
            self._log(f"读取 MCP stderr 失败：{exc}")

    def _stdout_thread(self, proc: Any, target: queue.Queue[Any]) -> None:
        try:
            if proc.stdout is None:
                return
            for line in proc.stdout:
                target.put(line)
        except Exception as exc:  # pragma: no cover - defensive
            self._log(f"读取 MCP stdout 失败：{exc}")
        finally:
            target.put(_EOF)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------
    async def _shutdown_transport(self, *, force: bool = False) -> None:
        proc = self._proc
        backend = self._backend
        self._proc = None
        self._backend = None
        self._initialized = False

        grace = 1.5 if force else 3.0
        try:
            if proc is not None:
                if backend == "asyncio":
                    await self._close_async_process(proc, grace=grace)
                elif backend == "thread":
                    await self._close_thread_process(proc, grace=grace)
        finally:
            await self._cancel_stderr_task()
            if self._stderr_thread is not None:
                await asyncio.to_thread(self._join_thread, self._stderr_thread)
                self._stderr_thread = None
            if self._reader_thread is not None:
                await asyncio.to_thread(self._join_thread, self._reader_thread)
                self._reader_thread = None
            self._queue = None
            self._thread_eof = False

    @staticmethod
    def _join_thread(thread: threading.Thread) -> None:
        try:
            thread.join(timeout=2.0)
        except Exception:
            pass

    async def _cancel_stderr_task(self) -> None:
        task = self._stderr_task
        self._stderr_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _close_async_process(self, proc: Any, *, grace: float) -> None:
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
                try:
                    await asyncio.wait_for(proc.stdin.wait_closed(), timeout=1.0)
                except (asyncio.TimeoutError, Exception):
                    pass
        except Exception:
            pass

        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
            return
        except (asyncio.TimeoutError, Exception):
            pass

        await self._terminate_then_kill_async(proc, grace=2.0)

    async def _terminate_then_kill_async(self, proc: Any, *, grace: float) -> None:
        try:
            proc.terminate()
        except (ProcessLookupError, Exception):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
            return
        except (asyncio.TimeoutError, Exception):
            pass
        try:
            proc.kill()
        except (ProcessLookupError, Exception):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
        except (asyncio.TimeoutError, Exception):
            self._log("MCP 进程在 kill 后仍未退出")

    async def _close_thread_process(self, proc: Any, *, grace: float) -> None:
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass

        try:
            await asyncio.to_thread(proc.wait, grace)
            return
        except (subprocess.TimeoutExpired, Exception):
            pass

        try:
            proc.terminate()
        except (ProcessLookupError, Exception):
            pass
        try:
            await asyncio.to_thread(proc.wait, 2.0)
            return
        except (subprocess.TimeoutExpired, Exception):
            pass
        try:
            proc.kill()
        except (ProcessLookupError, Exception):
            pass
        try:
            await asyncio.to_thread(proc.wait, 2.0)
        except (subprocess.TimeoutExpired, Exception):
            self._log("MCP 进程在 kill 后仍未退出")


def _humanize_tool_error(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return "MCP 工具调用失败"
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped[:800]
    if isinstance(payload, Mapping):
        for key in ("error", "message", "detail"):
            value = payload.get(key)
            if value:
                return str(value)[:800]
    return stripped[:800]


module_logger = logging.getLogger("mucage_ledger.mcp")
module_logger.addHandler(logging.NullHandler())
