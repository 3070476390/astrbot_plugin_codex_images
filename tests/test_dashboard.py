import asyncio
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_plugin import load_plugin


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.plugin = load_plugin(self.temp.name)
        self.plugin.config["account_mode"] = "shared"
        self.controller = self.plugin.dashboard
        self.key = self.plugin._account_key("caller")

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    async def test_settings_save_updates_live_runtime_and_disk_config(self):
        state = await self.controller.save_settings({"generation_timeout": 400, "allowed_users": ["qq:1", "qq:1"], "allow_group_images": True})
        self.assertEqual(self.plugin.service.settings.generation_timeout, 400)
        self.assertEqual(state["settings"]["allowed_users"], ["qq:1"])
        self.assertTrue(self.plugin.config.saved["allow_group_images"])

    async def test_invalid_settings_are_atomic(self):
        original = dict(self.plugin.config)
        for payload in [[], {"token": "secret"}, {"allow_group_images": "false"}, {"account_mode": "anything"},
                        {"allowed_users": ["bad"]}, {"allowed_users": ["qq:"]}, {"allowed_users": ["qq:1 2"]},
                        {"request_timeout": 0}, {"model": []}]:
            with self.subTest(payload=payload), self.assertRaises(Exception):
                await self.controller.save_settings(payload)
            self.assertEqual(dict(self.plugin.config), original)

    async def test_save_failure_rolls_back_memory(self):
        original = dict(self.plugin.config)
        with patch.object(self.plugin.config, "save_config", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(Exception, "保存失败"):
                await self.controller.save_settings({"model": "new"})
        self.assertEqual(dict(self.plugin.config), original)
        self.assertEqual(self.plugin.service.settings.model, "")

    async def test_active_task_blocks_settings_changes(self):
        self.plugin.image_owners[self.key] = "owner"
        with self.assertRaisesRegex(Exception, "任务正在运行"):
            await self.controller.save_settings({"account_mode": "personal"})

    async def test_background_login_survives_http_response_and_clears_code(self):
        gate = asyncio.Event()
        async def login(key):
            self.assertEqual(key, self.key)
            yield {"kind": "code", "url": "https://auth.openai.com/codex/device", "code": "TEST-1234"}
            await gate.wait()
            yield {"kind": "complete"}
        self.plugin.service.login = login
        state = await self.controller.start_login()
        self.assertTrue(state["login_pending"])
        await asyncio.sleep(0)
        self.assertEqual(self.controller.state()["account"]["code"], "TEST-1234")
        original_task = self.controller.login_task
        await self.controller.start_login()
        self.assertIs(self.controller.login_task, original_task)
        gate.set()
        await self.controller.login_task
        self.assertEqual(self.controller.state()["account"]["status"], "logged_in")
        self.assertNotIn("code", self.controller.state()["account"])

    async def test_cancel_closes_login_and_hides_device_code(self):
        cleaned = asyncio.Event()
        async def login(key):
            try:
                yield {"kind": "code", "url": "https://auth.openai.com/codex/device", "code": "SECRET"}
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        self.plugin.service.login = login
        await self.controller.start_login()
        await asyncio.sleep(0)
        state = await self.controller.cancel()
        self.assertTrue(cleaned.is_set())
        self.assertFalse(state["login_pending"])
        self.assertNotIn("SECRET", str(state))

    async def test_unexpected_login_failure_never_exposes_exception(self):
        async def login(key):
            raise RuntimeError("credential-secret")
            yield
        self.plugin.service.login = login
        await self.controller.start_login()
        await self.controller.login_task
        self.assertEqual(self.controller.state()["account"]["status"], "error")
        self.assertNotIn("credential-secret", str(self.controller.state()))

    async def test_logout_uses_shared_account(self):
        self.plugin.service.logout = AsyncMock()
        state = await self.controller.logout()
        self.plugin.service.logout.assert_awaited_once_with(self.key)
        self.assertEqual(state["account"]["status"], "signed_out")

    async def test_dashboard_routes_require_actual_dashboard_admin(self):
        web = types.ModuleType("astrbot.api.web")
        request = types.SimpleNamespace(username=None, json=AsyncMock(return_value={"model": "test"}))
        web.request = request
        web.json_response = lambda data, **kwargs: (200, data)
        web.error_response = lambda message, **kwargs: (kwargs.get("status_code", 400), message)
        api = types.ModuleType("astrbot.api")
        api.logger = types.SimpleNamespace(error=lambda *args: None)
        source = Path(__file__).resolve().parents[1] / "codex_images" / "web_api.py"
        with patch.dict(sys.modules, {"astrbot.api.web": web, "astrbot.api": api}):
            spec = importlib.util.spec_from_file_location("plugin_under_test.codex_images.web_api", source)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        routes = []
        context = types.SimpleNamespace(get_config=lambda: {"dashboard": {"username": "owner"}},
                                        register_web_api=lambda *args: routes.append(args))
        endpoint = module.DashboardAPI(context, self.controller)
        endpoint.register()
        self.assertEqual(len(routes), 6)
        for username in [None, "another-user", "api_key:123"]:
            request.username = username
            for handler in [endpoint.state, endpoint.refresh, endpoint.login, endpoint.logout, endpoint.cancel, endpoint.settings]:
                result = await handler()
                self.assertEqual(result[0], 403)
        request.json.assert_not_awaited()
        request.username = "owner"
        self.assertEqual((await endpoint.state())[0], 200)
        self.assertEqual((await endpoint.settings())[0], 200)
        self.assertEqual(self.plugin.service.settings.model, "test")
