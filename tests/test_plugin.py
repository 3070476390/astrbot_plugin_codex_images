"""Adapter tests with minimal AstrBot doubles; no running bot required."""
import importlib.util
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


class Image:
    def __init__(self, file="base64://fixture", url=""):
        self.file, self.url = file, url


class Reply:
    def __init__(self, chain=None):
        self.chain = chain


class Config(dict):
    def save_config(self):
        self.saved = dict(self)


def load_plugin(data_path):
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = logging.getLogger("test")
    event_module = types.ModuleType("astrbot.api.event")
    event_module.AstrMessageEvent = object
    event_module.filter = types.SimpleNamespace(command=lambda *a, **k: lambda f: f)
    star = types.ModuleType("astrbot.api.star")
    star.Star = type("Star", (), {"__init__": lambda self, context: None})
    star.Context = object
    paths = types.ModuleType("astrbot.core.utils.astrbot_path")
    paths.get_astrbot_data_path = lambda: data_path
    components = types.ModuleType("astrbot.api.message_components")
    components.Image, components.Reply = Image, Reply
    root = Path(__file__).resolve().parents[1]
    package = types.ModuleType("plugin_under_test")
    package.__path__ = [str(root)]
    modules = {"astrbot.api": api, "astrbot.api.event": event_module,
               "astrbot.api.star": star, "astrbot.core.utils.astrbot_path": paths,
               "astrbot.api.message_components": components,
               "plugin_under_test": package}
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location("plugin_under_test.main", root / "main.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        plugin_modules = {key: value for key, value in sys.modules.items() if key.startswith("plugin_under_test")}
    sys.modules.update(plugin_modules)
    return module.CodexImagesPlugin(None, Config(account_mode="personal"))


class Event:
    def __init__(self, text="", private=True, sender="1", platform="qq", messages=None):
        self.text, self.private, self.sender, self.platform = text, private, sender, platform
        self.sent = []
        self.messages = messages or []

    def is_private_chat(self):
        return self.private

    def is_admin(self):
        return self.sender == "admin"

    def get_platform_id(self):
        return self.platform

    def get_sender_id(self):
        return self.sender

    def get_message_str(self):
        return self.text

    def get_messages(self):
        return self.messages

    def plain_result(self, text):
        return text

    def image_result(self, path):
        assert Path(path).exists(), "image deleted before delivery"
        return ("image", path)

    async def send(self, value):
        self.sent.append(value)


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.plugin = load_plugin(self.temp.name)

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    async def test_group_login_denied_before_service(self):
        event = Event(private=False)
        results = [r async for r in self.plugin.login_command(event)]
        self.assertIn("私聊", results[0])
        self.assertFalse(self.plugin.service.sessions)

    async def test_allowlist_is_platform_scoped(self):
        self.plugin.config["allowed_users"] = ["qq:1"]
        results = [r async for r in self.plugin.status_command(Event(platform="discord"))]
        self.assertIn("使用名单", results[0])

    async def test_prompt_and_delivery_cleanup(self):
        job = Path(self.temp.name) / "image-job"
        job.mkdir()
        image = job / "image.png"
        image.write_bytes(b"image")
        self.plugin.service.generate = AsyncMock(return_value=[image])
        event = Event("codex画图 a cat  with spaces\nand lines")
        self.assertEqual([r async for r in self.plugin.image_command(event)], [])
        self.assertEqual(self.plugin.service.generate.call_args.args[1], "a cat  with spaces\nand lines")
        self.assertEqual(event.sent[-1][0], "image")
        self.assertFalse(image.exists())

    async def test_missing_prompt_does_not_start_generation(self):
        self.plugin.service.generate = AsyncMock()
        results = [r async for r in self.plugin.image_command(Event("codex画图"))]
        self.assertIn("用法", results[0])
        self.plugin.service.generate.assert_not_called()

    async def test_missing_identity_is_rejected(self):
        results = [r async for r in self.plugin.status_command(Event(sender=None))]
        self.assertIn("无法识别", results[0])

    async def test_groups_disabled_by_default(self):
        results = [r async for r in self.plugin.image_command(Event("codex画图 cat", private=False))]
        self.assertIn("仅允许私聊", results[0])

    async def test_shared_account_allowlist(self):
        self.plugin.config["account_mode"] = "shared"
        denied = [r async for r in self.plugin.image_command(Event("codex画图 cat"))]
        self.assertIn("使用名单", denied[0])
        self.plugin.config["allowed_users"] = ["qq:1"]
        self.plugin.service.generate = AsyncMock(return_value=[])
        await self._consume(self.plugin.image_command(Event("codex画图 cat")))
        self.assertNotEqual(self.plugin.service.generate.call_args.args[0], self.plugin._check(Event()))
        self.assertFalse(self.plugin.image_owners)

    async def _consume(self, generator):
        return [r async for r in generator]

    async def test_shared_chat_cannot_logout_or_login(self):
        self.plugin.config["account_mode"] = "shared"
        event = Event(sender="admin")
        for handler in [self.plugin.login_command, self.plugin.logout_command]:
            result = await self._consume(handler(event))
            self.assertIn("扩展功能", result[0])

    async def test_shared_cancel_cannot_stop_someone_elses_job(self):
        self.plugin.config.update(account_mode="shared", allowed_users=["qq:1", "qq:2"])
        owner = self.plugin._check(Event(sender="1"))
        key = self.plugin._account_key(owner)
        self.plugin.image_owners[key] = owner
        self.plugin.service.cancel = AsyncMock(return_value=True)
        denied = await self._consume(self.plugin.cancel_command(Event(sender="2")))
        self.assertIn("没有", denied[0])
        self.plugin.service.cancel.assert_not_called()
        await self._consume(self.plugin.cancel_command(Event(sender="1")))
        self.plugin.service.cancel.assert_awaited_once_with(key)

    async def test_shared_second_request_cannot_steal_owner(self):
        self.plugin.config.update(account_mode="shared", allowed_users=["qq:1", "qq:2"])
        owner = self.plugin._check(Event(sender="1"))
        key = self.plugin._account_key(owner)
        self.plugin.image_owners[key] = owner
        self.plugin.service.generate = AsyncMock()
        result = await self._consume(self.plugin.image_command(Event("codex画图 cat", sender="2")))
        self.assertIn("其他任务", result[0])
        self.assertEqual(self.plugin.image_owners[key], owner)
        self.plugin.service.generate.assert_not_called()

    async def test_cancel_during_progress_delivery_prevents_generation(self):
        self.plugin.config.update(account_mode="shared", allowed_users=["qq:1"])
        self.plugin.service.generate = AsyncMock()
        event = Event("codex画图 cat")
        async def delayed_send(value):
            await self._consume(self.plugin.cancel_command(Event()))
        event.send = delayed_send
        result = await self._consume(self.plugin.image_command(event))
        self.assertIn("取消", result[0])
        self.plugin.service.generate.assert_not_called()
        self.assertFalse(self.plugin.cancelled_images)

    async def test_edit_requires_one_image(self):
        self.plugin.service.generate = AsyncMock()
        for messages in [[], [Image(), Image()]]:
            event = Event("codex改图 改为水彩画", messages=messages)
            results = await self._consume(self.plugin.edit_command(event))
            self.assertTrue(results)
            self.plugin.service.generate.assert_not_called()

    async def test_attached_and_quoted_reference_are_passed(self):
        self.plugin.service.generate = AsyncMock(return_value=[])
        for handler, command, images in [
            (self.plugin.edit_command, "codex改图", [Image()]),
            (self.plugin.edit_command, "codex图生图", [Reply([Image()])]),
            (self.plugin.image_command, "codex画图", [Image()]),
        ]:
            with self.subTest(command=command):
                event = Event(command + " 改成水彩画", messages=images)
                await self._consume(handler(event))
                self.assertEqual(self.plugin.service.generate.call_args.args[1], "改成水彩画")
                self.assertTrue(callable(self.plugin.service.generate.call_args.kwargs["reference_loader"]))
                self.assertFalse(self.plugin.image_owners)

    async def test_direct_reference_takes_precedence_over_quote(self):
        event = Event("codex改图 水彩", messages=[Image(), Reply([Image(), Image()])])
        self.plugin.service.generate = AsyncMock(return_value=[])
        results = await self._consume(self.plugin.edit_command(event))
        self.assertEqual(results, [])
        self.plugin.service.generate.assert_awaited_once()

    async def test_model_command_pagination_and_capabilities(self):
        self.plugin.service.list_models = AsyncMock(return_value=[
            {"model": f"model-{i}", "display_name": f"Model {i}", "is_default": i == 0,
             "input_modalities": ["text", "image"] if i == 0 else ["text"]}
            for i in range(12)
        ])
        first = (await self._consume(self.plugin.models_command(Event("codex模型"))))[0]
        self.assertIn("1/2", first)
        self.assertIn("model-0", first)
        self.assertIn("默认", first)
        self.assertIn("图片输入", first)
        second = (await self._consume(self.plugin.models_command(Event("codex模型 2"))))[0]
        self.assertIn("model-11", second)
        self.assertNotIn("• model-0", second)

    async def test_model_lookup_requires_permission_and_valid_page(self):
        self.plugin.config.update(account_mode="shared", allowed_users=[])
        self.plugin.service.list_models = AsyncMock()
        await self._consume(self.plugin.models_command(Event("codex模型")))
        self.plugin.service.list_models.assert_not_called()
        for command in ["codex模型 abc", "codex模型 0", "codex模型 2 3"]:
            await self._consume(self.plugin.models_command(Event(command, sender="admin")))
            self.plugin.service.list_models.assert_not_called()
