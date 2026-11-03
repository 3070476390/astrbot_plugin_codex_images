"""Authenticated routes for AstrBot 4.29.4+ Plugin Views."""
from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

from .errors import PluginError


class DashboardAPI:
    def __init__(self, context, controller):
        self.context = context
        self.controller = controller

    def register(self):
        for endpoint, method, handler in (
            ("state", "GET", self.state), ("refresh", "POST", self.refresh),
            ("login", "POST", self.login), ("logout", "POST", self.logout),
            ("cancel", "POST", self.cancel), ("settings", "POST", self.settings),
            ("quota", "POST", self.quota),
        ):
            self.context.register_web_api(f"/astrbot_plugin_codex_images/dashboard/{endpoint}", handler, [method], "Codex 绘图管理")

    async def _run(self, operation, *, body=False):
        # Host validates the JWT. Additionally require the configured dashboard
        # administrator, rejecting generic API keys and other signed-in identities.
        admin = self.context.get_config().get("dashboard", {}).get("username")
        if not admin or request.username != admin:
            return error_response("仅 AstrBot 后台管理员可管理统一绘图账号。", status_code=403)
        try:
            data = await operation(await request.json(default=None)) if body else await operation()
            return json_response(data, headers={"Cache-Control": "no-store"})
        except PluginError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:
            logger.error("Codex 管理接口异常 (%s)", type(exc).__name__)
            return error_response("操作失败，请检查插件配置与 Codex 运行环境。", status_code=500)

    async def state(self):
        async def snapshot():
            return self.controller.state()
        return await self._run(snapshot)

    async def refresh(self):
        return await self._run(self.controller.refresh)

    async def quota(self):
        return await self._run(self.controller.refresh_quota)

    async def login(self):
        return await self._run(self.controller.start_login)

    async def logout(self):
        return await self._run(self.controller.logout)

    async def cancel(self):
        return await self._run(self.controller.cancel)

    async def settings(self):
        return await self._run(self.controller.save_settings, body=True)
