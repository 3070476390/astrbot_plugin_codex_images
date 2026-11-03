"""Dashboard state and operations, independent of AstrBot's HTTP framework."""
import asyncio
import copy
import json
from pathlib import Path

from .errors import PluginError
from .service import Settings, user_key

SHARED_KEY = user_key("__astrbot_codex_dashboard__", "shared")
SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(encoding="utf-8"))


class DashboardController:
    def __init__(self, plugin):
        self.plugin = plugin
        self.login_task = None
        self.lock = asyncio.Lock()
        self.account = {"status": "unknown", "message": "点击刷新检查统一账号的登录状态。"}

    @property
    def service(self):
        return self.plugin.service

    @property
    def login_pending(self):
        return self.login_task is not None and not self.login_task.done()

    def state(self):
        return {
            "version": "0.3.0",
            "settings": {k: copy.deepcopy(self.plugin.config.get(k, v["default"])) for k, v in SCHEMA.items()},
            "account": dict(self.account),
            "login_pending": self.login_pending,
            "busy": SHARED_KEY in self.service.sessions or SHARED_KEY in self.plugin.image_owners,
            "active_tasks": len(self.service.sessions),
        }

    async def refresh(self):
        async with self.lock:
            if not self.login_pending and SHARED_KEY not in self.plugin.image_owners:
                result = await self.service.status(SHARED_KEY)
                if not result.get("busy"):
                    self.account = {"status": "logged_in" if result["logged_in"] else "signed_out",
                                    "message": "账号已登录，图像权限以实际绘图结果为准。" if result["logged_in"] else "尚未登录统一账号。"}
            return self.state()

    async def start_login(self):
        async with self.lock:
            if self.plugin.account_mode != "shared":
                raise PluginError("请先保存为“统一绘图账号”模式。")
            if self.login_pending:
                return self.state()
            if SHARED_KEY in self.service.sessions or SHARED_KEY in self.plugin.image_owners:
                raise PluginError("统一账号正在执行任务，请等待完成或先取消。")
            self.account = {"status": "starting", "message": "正在获取官方设备码…"}
            self.login_task = asyncio.create_task(self._login())
            return self.state()

    async def _login(self):
        try:
            async for step in self.service.login(SHARED_KEY):
                if step["kind"] == "code":
                    self.account = {"status": "waiting", "message": "请在浏览器新标签页打开官方地址，输入设备码完成登录。",
                                    "url": step["url"], "code": step["code"]}
                else:
                    self.account = {"status": "logged_in", "message": "统一账号登录成功，可以向机器人发送绘图指令。"}
        except asyncio.CancelledError:
            self.account = {"status": "unknown", "message": "登录已取消，可刷新确认账号状态。"}
            raise
        except PluginError as exc:
            self.account = {"status": "error", "message": str(exc)}
        except Exception:
            self.account = {"status": "error", "message": "登录遇到内部错误，请检查 Codex 路径和运行环境。"}

    async def _stop_login(self):
        if self.login_pending:
            self.login_task.cancel()
            await asyncio.gather(self.login_task, return_exceptions=True)

    async def cancel(self):
        async with self.lock:
            await self._stop_login()
            await self.plugin.cancel_image(SHARED_KEY)
            # Remove stale device codes, without claiming logout occurred.
            self.account = {"status": "unknown", "message": "已取消任务；如需确认登录状态，请点击刷新。"}
            return self.state()

    async def logout(self):
        async with self.lock:
            await self._stop_login()
            if SHARED_KEY in self.plugin.image_owners:
                self.plugin.cancelled_images.add(SHARED_KEY)
            await self.service.logout(SHARED_KEY)
            self.account = {"status": "signed_out", "message": "统一账号已退出。"}
            return self.state()

    async def save_settings(self, payload):
        if not isinstance(payload, dict) or set(payload) - set(SCHEMA):
            raise PluginError("配置格式错误或包含不支持的字段。")
        candidate = self.state()["settings"]
        candidate.update(payload)
        for key, value in candidate.items():
            kind = SCHEMA[key]["type"]
            if kind == "string" and (not isinstance(value, str) or len(value) > 2048):
                raise PluginError(f"{key} 必须是有效字符串。")
            if kind == "bool" and not isinstance(value, bool):
                raise PluginError(f"{key} 必须是布尔值。")
        if candidate["account_mode"] not in {"shared", "personal"}:
            raise PluginError("不支持的账号模式。")
        allowed = candidate["allowed_users"]
        if not isinstance(allowed, list) or len(allowed) > 1000:
            raise PluginError("使用名单必须是列表，最多 1000 项。")
        for identity in allowed:
            if (not isinstance(identity, str) or len(identity) > 256 or ":" not in identity
                    or any(c.isspace() for c in identity) or not all(identity.split(":", 1))):
                raise PluginError("名单格式应为 平台实例ID:用户ID，每行一项。")
        candidate["allowed_users"] = list(dict.fromkeys(allowed))
        try:
            settings = Settings.from_config(candidate)
        except ValueError as exc:
            raise PluginError(str(exc)) from exc
        async with self.lock:
            if self.login_pending or self.service.sessions or self.plugin.image_owners:
                raise PluginError("有任务正在运行，请等待完成或取消后保存设置。")
            old = dict(self.plugin.config)
            try:
                self.plugin.config.update(candidate)
                # AstrBot saves this small configuration atomically and synchronously.
                self.plugin.config.save_config()
            except Exception as exc:
                self.plugin.config.clear()
                self.plugin.config.update(old)
                raise PluginError("配置保存失败，当前运行设置未更改。") from exc
            self.service.settings = settings
            return self.state()

    async def close(self):
        await self._stop_login()

