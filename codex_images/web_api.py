"""Authenticated routes for AstrBot 4.29.4+ Plugin Views."""
import asyncio

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request, stream_response

from .errors import PluginError
from .gallery import GALLERY_MIME


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
            ("gallery", "GET", self.gallery),
            ("gallery-preview", "GET", self.gallery_preview),
            ("gallery-download", "GET", self.gallery_download),
        ):
            self.context.register_web_api(f"/astrbot_plugin_codex_images/dashboard/{endpoint}", handler, [method], "Codex 绘图管理")

    async def _run(self, operation, *, body=False, raw=False):
        # Host validates the JWT. Additionally require the configured dashboard
        # administrator, rejecting generic API keys and other signed-in identities.
        admin = self.context.get_config().get("dashboard", {}).get("username")
        if not admin or request.username != admin:
            return error_response("仅 AstrBot 后台管理员可访问此功能。", status_code=403)
        try:
            data = await operation(await request.json(default=None)) if body else await operation()
            return data if raw else json_response(data, headers={"Cache-Control": "no-store"})
        except PluginError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:
            logger.error("Codex 管理接口异常 (%s)", type(exc).__name__)
            return error_response("操作失败，请检查插件配置和运行日志。", status_code=500)

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

    async def gallery(self):
        async def listing():
            gallery = self.controller.plugin.gallery
            result = await asyncio.to_thread(
                gallery.list, request.query.get("page", 1, type=int),
                request.query.get("backend", ""), request.query.get("q", ""),
            )
            return {**result, "enabled": self.controller.plugin.gallery_enabled}
        return await self._run(listing)

    async def gallery_preview(self):
        async def preview():
            return await asyncio.to_thread(self.controller.plugin.gallery.preview, request.query.get("id", ""))
        return await self._run(preview)

    async def gallery_download(self):
        async def download():
            item_id = request.query.get("id", "")
            data, extension = await asyncio.to_thread(self.controller.plugin.gallery.original, item_id)
            return stream_response(iter((data,)), content_type=GALLERY_MIME[extension], headers={
                "Content-Disposition": f'attachment; filename="codex-{item_id}{extension}"',
                "Content-Length": str(len(data)),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            })
        return await self._run(download, raw=True)
