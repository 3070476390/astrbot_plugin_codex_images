"""Adapter tests with minimal AstrBot doubles; no running bot required."""
import importlib.util
import asyncio
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image as PILImage


class Image:
    def __init__(self, file="base64://fixture", url=""):
        self.file, self.url = file, url


class Plain:
    def __init__(self, text):
        self.text = text


class Reply:
    def __init__(self, chain=None, message_str=None):
        self.chain = chain
        self.message_str = message_str


class Config(dict):
    def save_config(self):
        self.saved = dict(self)


class FakeStar:
    def __init__(self, context):
        self.kv = {}

    async def get_kv_data(self, key, default):
        return self.kv.get(key, default)

    async def put_kv_data(self, key, value):
        self.kv[key] = value


def load_plugin(data_path):
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = logging.getLogger("test")
    event_module = types.ModuleType("astrbot.api.event")
    event_module.AstrMessageEvent = object
    event_module.filter = types.SimpleNamespace(command=lambda *a, **k: lambda f: f)
    star = types.ModuleType("astrbot.api.star")
    star.Star = FakeStar
    star.Context = object
    paths = types.ModuleType("astrbot.core.utils.astrbot_path")
    paths.get_astrbot_data_path = lambda: data_path
    components = types.ModuleType("astrbot.api.message_components")
    components.Image, components.Plain, components.Reply = Image, Plain, Reply
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
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        plugin_modules = {key: value for key, value in sys.modules.items() if key.startswith("plugin_under_test")}
    sys.modules.update(plugin_modules)
    return module.CodexImagesPlugin(None, Config(account_mode="personal"))


class Event:
    def __init__(self, text="", private=True, sender="1", platform="qq", messages=None, group="group"):
        self.text, self.private, self.sender, self.platform = text, private, sender, platform
        self.sent = []
        self.messages = messages or []
        self.unified_msg_origin = f"{platform}:private:{sender}" if private else f"{platform}:group:{group}"

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
        return types.SimpleNamespace(chain=[Image(file=path)])

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
        with PILImage.new("RGB", (12, 8), "blue") as output:
            output.save(image)
        self.plugin.service.generate = AsyncMock(return_value=[image])
        event = Event("codex画图 a cat  with spaces\nand lines")
        self.assertEqual([r async for r in self.plugin.image_command(event)], [])
        self.assertEqual(self.plugin.service.generate.call_args.args[1], "a cat  with spaces\nand lines")
        self.assertIsInstance(event.sent[-1].chain[0], Image)
        self.assertFalse(image.exists())
        item, = self.plugin.gallery.list(1, "codex", "cat")["items"]
        self.assertEqual(item["prompt"], "a cat  with spaces\nand lines")

    async def test_gallery_failure_or_opt_out_keeps_image_delivery_working(self):
        for enabled in (True, False):
            self.plugin.config["gallery_enabled"] = enabled
            job = Path(self.temp.name) / "gallery-error-job"
            job.mkdir()
            image = job / "image.png"
            image.write_bytes(b"mock image")
            self.plugin.service.generate = AsyncMock(return_value=[image])
            event = Event("codex画图 gallery")
            with patch.object(self.plugin.gallery, "save", side_effect=OSError("disk error")) as save:
                with self.assertLogs("test", level="WARNING") if enabled else self.assertNoLogs("test"):
                    self.assertEqual(await self._consume(self.plugin.image_command(event)), [])
                self.assertEqual(save.called, enabled)
            self.assertIsInstance(event.sent[-1].chain[0], Image)
            self.assertFalse(image.exists())

    async def test_missing_prompt_does_not_start_generation(self):
        self.plugin.service.generate = AsyncMock()
        results = [r async for r in self.plugin.image_command(Event("codex画图"))]
        self.assertIn("用法", results[0])
        self.plugin.service.generate.assert_not_called()

    async def test_ratio_option_preserves_prompt_and_rejects_invalid_requests(self):
        self.plugin.service.generate = AsyncMock(return_value=[])
        for command in ("codex画图 --比例 4:3 cat", "codex画图 --比例", "codex画图 --比例 1:1"):
            self.assertTrue(await self._consume(self.plugin.image_command(Event(command))))
        self.plugin.service.generate.assert_not_awaited()
        event = Event("codex画图 --比例=16：9  a cat\nwith  spaces")
        await self._consume(self.plugin.image_command(event))
        self.plugin.service.generate.assert_awaited_once_with(
            self.plugin._check(event), "a cat\nwith  spaces", ratio="16:9",
        )

    async def test_redraw_reuses_original_edit_and_is_scoped_to_owner_and_chat(self):
        self.plugin.config.update(account_mode="shared", allow_all_users=True, allow_group_images=True)
        self.plugin.service.settings.model = "original-model"
        reference = Image()
        original_bytes = b"original reference bytes"
        received = []

        async def generate(key, prompt, *, reference_loader, **kwargs):
            received.append((key, prompt, await reference_loader(), kwargs))
            job = Path(self.temp.name) / f"job-{len(received)}"
            job.mkdir()
            output = job / "image.png"
            with PILImage.new("RGB", (12, 8), "blue") as image:
                image.save(output)
            return [output]

        self.plugin.service.generate = AsyncMock(side_effect=generate)
        event = Event("codex改图 --比例 9:16 改成水彩", private=False, messages=[reference])
        main = sys.modules["plugin_under_test.main"]
        with patch.object(main, "load_reference", new=AsyncMock(return_value=original_bytes)) as load:
            self.assertEqual(await self._consume(self.plugin.edit_command(event)), [])
            label = event.sent[-1].chain[-1].text
            drawing_id = label.rsplit(" ", 1)[-1]
            self.plugin.service.settings.model = "updated-model"

            for denied in (
                Event(f"codex重绘 {drawing_id}", private=False, sender="2"),
                Event(f"codex重绘 {drawing_id}", private=False, group="another-group"),
                Event(f"codex重绘 {drawing_id}", private=False, platform="another-bot"),
            ):
                self.assertIn("找不到", (await self._consume(self.plugin.redraw_command(denied)))[0])
            self.plugin.service.generate.assert_awaited_once()

            quoted = Event("codex重绘", private=False, messages=[Reply([Plain(label), Image()])])
            by_id = Event(f"codex重绘 {drawing_id}", private=False)
            for retry in (quoted, by_id):
                self.assertEqual(await self._consume(self.plugin.redraw_command(retry)), [])
                self.assertEqual(retry.sent[-1].chain[-1].text, label)
            load.assert_awaited_once()
        self.assertEqual(len(received), 3)
        self.assertEqual(received[0], received[1])
        self.assertEqual(received[0], received[2])
        self.assertEqual(received[0][3], {"ratio": "9:16", "model": "original-model"})
        self.assertFalse(list(Path(self.temp.name).glob("job-*")))

    async def test_mode_menu_marks_only_the_current_mode(self):
        result, = await self._consume(self.plugin.mode_command(Event("codex模式")))
        self.assertIn("1. Codex 订阅 ✓", result.splitlines())
        self.assertIn("2. OpenAI API", result.splitlines())
        self.assertEqual(result.count("✓"), 1)
        self.assertIn("/codex模式", result)
        self.assertFalse(self.plugin.kv)

    async def test_numeric_switch_updates_only_the_requesting_users_mode(self):
        await self.plugin.dashboard.save_settings({
            "openai_api_enabled": True, "openai_api_key": "test-only-key",
        })
        event = Event("codex模式 2")
        caller = self.plugin._check(event)
        await self._consume(self.plugin.mode_command(event))
        self.assertEqual(await self.plugin._backend(caller), "api")

        result, = await self._consume(self.plugin.mode_command(Event("codex模式")))
        self.assertIn("1. Codex 订阅", result.splitlines())
        self.assertIn("2. OpenAI API ✓", result.splitlines())
        self.assertEqual(result.count("✓"), 1)

        other, = await self._consume(self.plugin.mode_command(Event("codex模式", sender="2")))
        self.assertIn("1. Codex 订阅 ✓", other.splitlines())
        self.assertEqual(other.count("✓"), 1)

        await self._consume(self.plugin.mode_command(Event("codex模式 1  ")))
        self.assertEqual(await self.plugin._backend(caller), "codex")

    async def test_numeric_switch_rejects_invalid_or_unavailable_modes(self):
        unavailable, = await self._consume(self.plugin.mode_command(Event("codex模式 2")))
        self.assertIn("尚未启用", unavailable)
        self.plugin.config["openai_api_enabled"] = True
        missing_key, = await self._consume(self.plugin.mode_command(Event("codex模式 2")))
        self.assertIn("尚未配置", missing_key)
        invalid, = await self._consume(self.plugin.mode_command(Event("codex模式 3")))
        self.assertIn("/codex模式", invalid)
        self.assertFalse(self.plugin.kv)

    async def test_api_mode_is_opt_in_and_preserves_the_original_backend_on_redraw(self):
        self.plugin.service.generate = AsyncMock(return_value=[])
        self.plugin.service.generate_api = AsyncMock()
        select_api = Event("codex模式 API")
        denied = await self._consume(self.plugin.mode_command(select_api))
        self.assertIn("尚未启用", denied[0])
        self.plugin.config["openai_api_enabled"] = True
        missing_key = await self._consume(self.plugin.mode_command(select_api))
        self.assertIn("尚未配置", missing_key[0])
        await self._consume(self.plugin.image_command(Event("codex画图 first")))
        self.plugin.service.generate.assert_awaited_once()

        def output():
            job = Path(self.temp.name) / "api-result"
            job.mkdir()
            image = job / "image.png"
            with PILImage.new("RGB", (12, 8), "blue") as generated:
                generated.save(image)
            return [image]

        await self.plugin.dashboard.save_settings({"openai_api_key": "test-only-key"})
        selected = await self._consume(self.plugin.mode_command(select_api))
        self.assertIn("API 项目", selected[0])
        other = await self._consume(self.plugin.mode_command(Event("codex模式", sender="2")))
        self.assertIn("Codex", other[0])
        self.plugin.service.generate_api.return_value = output()
        event = Event("codex画图 --比例 1:1 second")
        self.assertEqual(await self._consume(self.plugin.image_command(event)), [])
        drawing_id = event.sent[-1].chain[-1].text.rsplit(" ", 1)[-1]
        self.assertIn("按量计费", event.sent[0])
        expected = {"ratio": "1:1", "model": self.plugin.service.settings.openai_image_model, "quality": "medium"}
        caller = self.plugin._check(event)
        self.plugin.service.generate_api.assert_awaited_once_with(caller, "second", **expected)

        self.plugin.config["openai_api_enabled"] = False
        blocked = await self._consume(self.plugin.image_command(Event("codex画图 blocked")))
        self.assertIn("尚未启用", blocked[0])
        await self._consume(self.plugin.mode_command(Event("codex模式 Codex")))
        redraw = Event(f"codex重绘 {drawing_id}")
        wrong_mode = await self._consume(self.plugin.redraw_command(redraw))
        self.assertIn("原出图模式", wrong_mode[0])
        self.plugin.service.generate.assert_awaited_once()
        self.plugin.service.generate_api.assert_awaited_once()

        self.plugin.config["openai_api_enabled"] = True
        await self._consume(self.plugin.mode_command(select_api))
        self.plugin.service.settings.openai_image_quality = "high"
        self.plugin.service.generate_api.return_value = output()
        self.assertEqual(await self._consume(self.plugin.redraw_command(redraw)), [])
        self.plugin.service.generate_api.assert_awaited_with(caller, "second", **expected)
        self.assertEqual(self.plugin.gallery.list(1, "api", "second")["total"], 2)

    async def test_api_cancellation_is_limited_to_the_request_owner(self):
        await self.plugin.dashboard.save_settings({
            "openai_api_enabled": True, "account_mode": "shared", "allow_all_users": True,
            "openai_api_key": "test-only-key",
        })
        started = asyncio.Event()

        async def generate(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        self.plugin.service.api.generate = generate
        await self._consume(self.plugin.mode_command(Event("codex模式 API")))
        request = asyncio.create_task(self._consume(self.plugin.image_command(Event("codex画图 cat"))))
        await asyncio.wait_for(started.wait(), 1)
        self.assertEqual(self.plugin.dashboard.state()["active_tasks"], 1)
        denied = await self._consume(self.plugin.cancel_command(Event("codex取消", sender="2")))
        self.assertIn("没有", denied[0])
        self.assertFalse(request.done())
        await self._consume(self.plugin.mode_command(Event("codex模式 Codex")))
        self.assertIn("停止", (await self._consume(self.plugin.cancel_command(Event("codex取消"))))[0])
        self.assertIn("取消", (await asyncio.wait_for(request, 1))[0])
        self.assertFalse(self.plugin.image_owners)
        self.assertEqual(self.plugin.dashboard.state()["active_tasks"], 0)

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

    async def test_all_users_can_draw_with_shared_account_and_restore_allowlist(self):
        self.plugin.config.update(account_mode="shared", allow_all_users=True, allowed_users=["qq:allowed"])
        self.plugin.service.generate = AsyncMock(return_value=[])
        visitor = Event("codex画图 cat", platform="another-bot", sender="visitor")
        self.assertEqual(await self._consume(self.plugin.image_command(visitor)), [])
        caller = self.plugin._check(visitor)
        self.plugin.service.generate.assert_awaited_once_with(self.plugin._account_key(caller), "cat")
        self.assertNotEqual(caller, self.plugin._account_key(caller))
        self.plugin.service.generate.reset_mock()
        self.plugin.config["allow_all_users"] = False
        denied = await self._consume(self.plugin.image_command(visitor))
        self.assertIn("使用名单", denied[0])
        self.plugin.service.generate.assert_not_awaited()
        self.assertEqual(self.plugin.config["allowed_users"], ["qq:allowed"])
        await self._consume(self.plugin.image_command(Event("codex画图 cat", sender="allowed")))
        self.plugin.service.generate.assert_awaited_once()

    async def test_all_users_personal_accounts_remain_separate(self):
        self.plugin.config.update(account_mode="personal", allow_all_users=True, allowed_users=["qq:allowed"])
        self.plugin.service.generate = AsyncMock(return_value=[])
        for sender in ("visitor-1", "visitor-2"):
            event = Event("codex画图 cat", sender=sender)
            self.assertEqual(await self._consume(self.plugin.image_command(event)), [])
            self.assertEqual(self.plugin.service.generate.call_args.args[0], self.plugin._check(event))
        calls = self.plugin.service.generate.await_args_list
        self.assertNotEqual(calls[0].args[0], calls[1].args[0])

    async def test_all_users_still_requires_identity_and_separate_group_access(self):
        self.plugin.config.update(account_mode="shared", allow_all_users=True)
        for event, message in [(Event(sender=None), "无法识别"), (Event(platform=None), "无法识别"), (Event(private=False), "私聊")]:
            with self.subTest(message=message), self.assertRaisesRegex(Exception, message):
                self.plugin._check(event)
        self.plugin.config["allow_group_images"] = True
        self.assertTrue(self.plugin._check(Event(private=False)))
        with self.assertRaisesRegex(Exception, "账号操作请私聊"):
            self.plugin._check(Event(private=False), private=True)
        for invalid in (None, "true", "false", 1):
            self.plugin.config["allow_all_users"] = invalid
            with self.subTest(invalid=invalid), self.assertRaisesRegex(Exception, "使用名单"):
                self.plugin._check(Event())

    async def test_all_users_cannot_manage_shared_login_or_cancel_others(self):
        self.plugin.config.update(account_mode="shared", allow_all_users=True)
        self.plugin.service.login = AsyncMock()
        self.plugin.service.logout = AsyncMock()
        for handler in (self.plugin.login_command, self.plugin.logout_command):
            result = await self._consume(handler(Event()))
            self.assertIn("扩展功能", result[0])
        self.plugin.service.login.assert_not_called()
        self.plugin.service.logout.assert_not_awaited()
        owner = self.plugin._check(Event(sender="2"))
        key = self.plugin._account_key(owner)
        self.plugin.image_owners[key] = owner
        self.plugin.service.cancel = AsyncMock(return_value=True)
        result = await self._consume(self.plugin.cancel_command(Event(sender="1")))
        self.assertIn("没有", result[0])
        self.plugin.service.cancel.assert_not_awaited()
        await self._consume(self.plugin.cancel_command(Event(sender="2")))
        self.plugin.service.cancel.assert_awaited_once_with(key)

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
        self.assertRegex(first, r"(?m)^1\. model-0.*✓")
        self.assertEqual(first.count("✓"), 1)
        self.assertIn("默认", first)
        self.assertIn("图片输入", first)
        second = (await self._consume(self.plugin.models_command(Event("codex模型 页 2"))))[0]
        self.assertRegex(second, r"(?m)^12\. model-11\b")
        self.assertNotRegex(second, r"(?m)^1\. model-0\b")

    async def test_numbered_codex_model_keeps_the_displayed_id_and_user_scope(self):
        self.plugin.config.update(account_mode="shared", allow_all_users=True, allow_group_images=True)
        self.plugin.service.settings.model = "base-model"
        models = [
            {"model": model, "is_default": default, "input_modalities": ["image"]}
            for model, default in [("base-model", True), ("selected-model", False)]
        ]
        self.plugin.service.list_models = AsyncMock(return_value=models)
        self.plugin.service.generate = AsyncMock(return_value=[])
        await self._consume(self.plugin.models_command(Event("codex模型", private=False)))
        self.plugin.service.list_models.return_value = list(reversed(models))

        for event in (
            Event("codex模型 2", private=False, sender="2"),
            Event("codex模型 2", private=False, group="other-group"),
        ):
            result, = await self._consume(self.plugin.models_command(event))
            self.assertIn("/codex模型", result)
        self.assertFalse(self.plugin.kv)

        selected, = await self._consume(self.plugin.models_command(Event("codex模型 2", private=False)))
        self.assertIn("selected-model", selected)
        self.assertEqual(self.plugin.service.settings.model, "base-model")
        listing, = await self._consume(self.plugin.models_command(Event("codex模型", private=False)))
        self.assertRegex(listing, r"(?m)^\d+\. selected-model.*✓")
        self.assertEqual(listing.count("✓"), 1)

        for sender, model in (("1", "selected-model"), ("2", "base-model")):
            await self._consume(self.plugin.image_command(Event("codex画图 cat", sender=sender)))
            self.assertEqual(self.plugin.service.generate.await_args.kwargs["model"], model)

    async def test_removed_model_number_preserves_the_current_model(self):
        base = {"model": "base-model", "is_default": True, "input_modalities": ["image"]}
        removed = {"model": "removed-model", "is_default": False, "input_modalities": ["image"]}
        replacement = {"model": "replacement-model", "is_default": False, "input_modalities": ["image"]}
        self.plugin.service.list_models = AsyncMock(side_effect=[[base, removed], [base, replacement]])
        await self._consume(self.plugin.models_command(Event("codex模型")))
        result, = await self._consume(self.plugin.models_command(Event("codex模型 2")))
        self.assertIn("/codex模型", result)
        self.assertFalse(self.plugin.kv)

    async def test_numbered_api_model_switch_reset_and_redraw(self):
        default, selected = "gpt-image-2", "gpt-image-2.5-flare"
        await self.plugin.dashboard.save_settings({
            "openai_api_enabled": True, "openai_api_key": "test-only-key",
            "openai_image_model": default,
        })
        self.plugin.service.api.list_models = AsyncMock(return_value=[default, selected])
        self.plugin.service.list_models = AsyncMock()
        await self._consume(self.plugin.mode_command(Event("codex模式 2")))
        listing, = await self._consume(self.plugin.models_command(Event("codex模型")))
        self.assertRegex(listing, rf"(?m)^1\. {default}.*✓")
        self.assertRegex(listing, rf"(?m)^2\. {selected}\b")
        result, = await self._consume(self.plugin.models_command(Event("codex模型 2")))
        self.assertIn(selected, result)
        self.assertEqual(self.plugin.service.settings.openai_image_model, default)

        async def render(*args, **kwargs):
            job = Path(self.temp.name) / "model-choice"
            job.mkdir()
            image = job / "image.png"
            with PILImage.new("RGB", (12, 8), "blue") as generated:
                generated.save(image)
            return [image]

        self.plugin.service.generate_api = AsyncMock(side_effect=render)
        image_request = Event("codex画图 cat")
        self.assertEqual(await self._consume(self.plugin.image_command(image_request)), [])
        self.assertEqual(self.plugin.service.generate_api.await_args.kwargs["model"], selected)
        drawing_id = image_request.sent[-1].chain[-1].text.rsplit(" ", 1)[-1]

        await self._consume(self.plugin.models_command(Event("codex模型 默认")))
        await self._consume(self.plugin.image_command(Event("codex画图 cat")))
        self.assertEqual(self.plugin.service.generate_api.await_args.kwargs["model"], default)
        await self._consume(self.plugin.redraw_command(Event(f"codex重绘 {drawing_id}")))
        self.assertEqual(self.plugin.service.generate_api.await_args.kwargs["model"], selected)
        self.plugin.service.list_models.assert_not_awaited()

    async def test_model_lookup_requires_permission_and_valid_page(self):
        self.plugin.config.update(account_mode="shared", allowed_users=[])
        self.plugin.service.list_models = AsyncMock()
        await self._consume(self.plugin.models_command(Event("codex模型")))
        self.plugin.service.list_models.assert_not_called()
        for command in ["codex模型 abc", "codex模型 0", "codex模型 2 3"]:
            await self._consume(self.plugin.models_command(Event(command, sender="admin")))
            self.plugin.service.list_models.assert_not_called()

    async def test_quota_command_permissions_and_account_selection(self):
        from codex_images.quota import normalize_quota
        self.plugin.service.read_quota = AsyncMock(return_value=normalize_quota({"rateLimits": {}}))
        own_key = self.plugin._check(Event())
        reply = (await self._consume(self.plugin.quota_command(Event("codex额度"))))[0]
        self.assertIn("我的账号", reply)
        self.plugin.service.read_quota.assert_awaited_once_with(own_key)
        self.plugin.service.read_quota.reset_mock()
        self.plugin.config.update(account_mode="shared", allowed_users=[])
        reply = (await self._consume(self.plugin.quota_command(Event("codex额度"))))[0]
        self.assertIn("使用名单", reply)
        self.plugin.service.read_quota.assert_not_awaited()
        self.plugin.config["allowed_users"] = ["qq:1"]
        reply = (await self._consume(self.plugin.quota_command(Event("codex额度"))))[0]
        self.assertIn("统一账号", reply)
        self.plugin.service.read_quota.assert_awaited_once_with(self.plugin._account_key(own_key))

    async def test_quota_group_and_busy_checks_precede_service(self):
        self.plugin.service.read_quota = AsyncMock()
        reply = (await self._consume(self.plugin.quota_command(Event(private=False))))[0]
        self.assertIn("私聊", reply)
        self.plugin.image_owners[self.plugin._check(Event())] = "owner"
        reply = (await self._consume(self.plugin.quota_command(Event())))[0]
        self.assertIn("等待完成", reply)
        self.plugin.service.read_quota.assert_not_awaited()
