"""A bounded, asynchronous client for Codex app-server's JSONL protocol."""

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from .errors import PluginError, RpcError

MAX_MESSAGE_BYTES = 48 * 1024 * 1024
NOTIFICATIONS = {"account/login/completed", "item/completed", "turn/completed"}


def child_environment(home: Path) -> dict[str, str]:
    # Do not inherit the bot's API keys, Codex session IDs or auth overrides.
    allowed = {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
        "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "LANG", "LC_ALL",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
    }
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    env["CODEX_HOME"] = str(home)
    return env


class AppServer:
    def __init__(self, executable: str, home: Path, cwd: Path, timeout: float = 30):
        self.executable = executable
        self.home = home
        self.cwd = cwd
        self.timeout = timeout
        self.process = None
        self.pending: dict[int, asyncio.Future] = {}
        self.events: asyncio.Queue = asyncio.Queue(maxsize=128)
        self.reader = None
        self.stderr_reader = None
        self.sequence = 0
        self.write_lock = asyncio.Lock()
        self.close_lock = asyncio.Lock()
        self.closed = False

    async def start(self):
        executable = shutil.which(self.executable)
        if not executable:
            raise PluginError("找不到 Codex。请在 AstrBot 所在环境安装 Codex CLI，并设置 codex_command。")
        if os.name == "nt" and Path(executable).suffix.lower() in {".cmd", ".bat", ".ps1"}:
            raise PluginError("Windows 请将 codex_command 设置为原生 codex.exe 的完整路径。")
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        try:
            self.process = await asyncio.create_subprocess_exec(
                executable, "app-server", "--listen", "stdio://",
                cwd=str(self.cwd), env=child_environment(self.home),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=MAX_MESSAGE_BYTES, **kwargs,
            )
        except OSError as exc:
            raise PluginError("无法启动 Codex，请检查可执行文件路径和运行权限。") from exc
        if self.closed:
            self.process.terminate()
            await self.process.wait()
            raise PluginError("任务已取消。")
        self.reader = asyncio.create_task(self._read())
        self.stderr_reader = asyncio.create_task(self._drain_stderr())
        try:
            await self.request("initialize", {
                "clientInfo": {"name": "astrbot_codex_images", "version": "0.3.0"},
                "capabilities": {"experimentalApi": True},
            })
            await self._write({"method": "initialized", "params": {}})
        except BaseException:
            await self.close()
            raise

    async def _write(self, message):
        async with self.write_lock:
            if self.closed or not self.process or self.process.returncode is not None:
                raise PluginError("Codex 连接已关闭，请重新尝试。")
            try:
                self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
                await self.process.stdin.drain()
            except (BrokenPipeError, ConnectionError) as exc:
                raise PluginError("Codex 进程已断开连接。") from exc

    async def request(self, method: str, params=None):
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self._write({"id": request_id, "method": method, "params": params or {}})
            reply = await asyncio.wait_for(future, self.timeout)
            if "error" in reply:
                raise RpcError(method, reply["error"].get("code"))
            return reply.get("result", {})
        except asyncio.TimeoutError as exc:
            raise PluginError(f"Codex 请求 {method} 超时，请检查网络后重试。") from exc
        finally:
            self.pending.pop(request_id, None)

    def _fail(self):
        for future in self.pending.values():
            if not future.done():
                future.set_exception(PluginError("Codex 连接已关闭或协议异常，请检查版本后重试。"))
        # Always wake a notification consumer, even when the queue is full.
        if self.events.full():
            self.events.get_nowait()
        self.events.put_nowait(None)

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("invalid protocol message")
                if "method" in message and "id" in message:
                    # This integration cannot approve shell access, file writes,
                    # arbitrary tools, input requests, or token refresh requests.
                    await self._write({"id": message["id"], "error": {
                        "code": -32601, "message": "Client does not support this request",
                    }})
                elif "id" in message:
                    future = self.pending.get(message["id"])
                    if future and not future.done():
                        future.set_result(message)
                elif message.get("method") in NOTIFICATIONS:
                    self.events.put_nowait(message)
        except (ValueError, asyncio.QueueFull, OSError, PluginError):
            pass
        finally:
            self._fail()

    async def _drain_stderr(self):
        # Drain without logging: upstream diagnostics can contain credentials.
        while await self.process.stderr.read(8192):
            pass

    async def next_event(self):
        event = await self.events.get()
        if event is None:
            raise PluginError("Codex 连接已中断，请重新尝试。")
        return event

    async def close(self):
        async with self.close_lock:
            await self._close()

    async def _close(self):
        if self.closed:
            return
        self.closed = True
        self._fail()
        if self.process and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.process.kill()
                await self.process.wait()
        for task in (self.reader, self.stderr_reader):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self.reader, self.stderr_reader) if t), return_exceptions=True)
