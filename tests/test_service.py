import asyncio
import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_images.errors import PluginError
from codex_images.images import save_image
from codex_images.rpc import child_environment
from codex_images.service import ImageService, Settings, user_key

PNG = b"\x89PNG\r\n\x1a\nfixture"


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.key = user_key("qq_instance", "123")
        self.mode = "success"
        self.processes = []
        original = asyncio.create_subprocess_exec

        async def launch(*args, **kwargs):
            kwargs["env"]["FAKE_MODE"] = self.mode
            process = await original(sys.executable, str(Path(__file__).with_name("fake_codex.py")), **kwargs)
            self.processes.append(process)
            return process

        self.patcher = patch("codex_images.rpc.asyncio.create_subprocess_exec", launch)
        self.patcher.start()
        self.service = ImageService(self.root, Settings(executable=sys.executable, request_timeout=2))

    async def asyncTearDown(self):
        await self.service.close()
        self.patcher.stop()
        for process in self.processes:
            self.assertIsNotNone(process.returncode, "orphaned Codex subprocess")
        self.temp.cleanup()

    async def test_generation_handles_early_duplicate_and_foreign_events(self):
        paths = await self.service.generate(self.key, "猫咪 with spaces\nand lines")
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0].read_bytes(), PNG)
        self.assertIn(self.key, str(paths[0]))
        self.assertFalse(self.service.sessions)

    async def test_device_login_early_notification_and_status(self):
        self.mode = "login"
        steps = [step async for step in self.service.login(self.key)]
        self.assertEqual([s["kind"] for s in steps], ["code", "complete"])
        self.assertNotIn("secret", str(steps))

    async def test_already_logged_in(self):
        steps = [step async for step in self.service.login(self.key)]
        self.assertEqual(steps, [{"kind": "complete", "already": True}])

    async def test_untrusted_login_url_rejected(self):
        self.mode = "bad_url"
        with self.assertRaisesRegex(PluginError, "官方设备码"):
            async for _ in self.service.login(self.key):
                self.fail("untrusted link was returned")

    async def test_anonymous_does_not_start_generation(self):
        self.mode = "anonymous"
        with self.assertRaisesRegex(PluginError, "尚未登录"):
            await self.service.generate(self.key, "cat")

    async def test_login_timeout_closes_process(self):
        self.mode = "wait_login"
        self.service.settings.login_timeout = 0.05
        with self.assertRaisesRegex(PluginError, "登录等待超时"):
            async for _ in self.service.login(self.key):
                pass
        self.assertFalse(self.service.sessions)

    async def test_generation_timeout_closes_process(self):
        self.mode = "wait_image"
        self.service.settings.generation_timeout = 0.05
        with self.assertRaisesRegex(PluginError, "图像生成超时"):
            await self.service.generate(self.key, "cat")

    async def test_cancellation_and_busy_status(self):
        self.mode = "wait_image"
        task = asyncio.create_task(self.service.generate(self.key, "cat"))
        while not self.service.sessions:
            await asyncio.sleep(0)
        self.assertEqual(await self.service.status(self.key), {"busy": True})
        # Wait for the process to start to exercise in-flight RPC cancellation.
        while not self.processes:
            await asyncio.sleep(0.01)
        self.assertTrue(await self.service.cancel(self.key))
        with self.assertRaisesRegex(PluginError, "取消"):
            await task
        self.assertFalse(self.service.sessions)

    async def test_same_user_and_global_concurrency_limits(self):
        self.service.settings.max_sessions = 1
        async with self.service.session(self.key):
            with self.assertRaisesRegex(PluginError, "已有"):
                await self.service.generate(self.key, "cat")
            with self.assertRaisesRegex(PluginError, "已满"):
                await self.service.generate(user_key("qq_instance", "456"), "cat")

    async def test_server_requests_are_denied(self):
        self.mode = "approval"
        self.assertEqual(len(await self.service.generate(self.key, "cat")), 1)

    async def test_failures_and_protocol_changes_are_not_reported_as_success(self):
        for mode in ["crash", "rpc_error", "no_image", "quota", "file_id", "bad_image", "failed_turn"]:
            with self.subTest(mode=mode):
                self.mode = mode
                with self.assertRaises(PluginError) as raised:
                    await self.service.generate(self.key, "cat")
                self.assertNotIn("secret-token", str(raised.exception))
                self.assertFalse(self.service.sessions)
        self.assertFalse(list(self.root.glob("users/*/jobs/*/*.png")))

    async def test_saved_path(self):
        self.mode = "saved_path"
        paths = await self.service.generate(self.key, "cat")
        self.assertEqual(paths[0].read_bytes(), PNG)

    async def test_prompt_limit_before_process_start(self):
        with self.assertRaises(PluginError):
            await self.service.generate(self.key, "a" * 4001)
        self.assertFalse(self.processes)

    async def test_logout(self):
        await self.service.logout(self.key)
        self.assertFalse(self.service.sessions)

    async def test_image_edit_sends_private_reference_and_cleans_it(self):
        self.mode = "edit"
        async def loader():
            return PNG
        output = await self.service.generate(self.key, "改成水彩画", reference_loader=loader)
        self.assertEqual(output[0].read_bytes(), PNG)
        self.assertFalse(list(self.root.glob("users/*/jobs/*/reference.*")))

    async def test_reference_rejected_and_removed_on_failure(self):
        async def invalid():
            return b"not an image"
        with self.assertRaises(PluginError):
            await self.service.generate(self.key, "watercolor", reference_loader=invalid)
        self.mode = "crash"
        async def valid():
            return PNG
        with self.assertRaises(PluginError):
            await self.service.generate(self.key, "watercolor", reference_loader=valid)
        self.assertFalse(list(self.root.glob("users/*/jobs/*/reference.*")))

    async def test_cancel_during_reference_loading_stops_loader(self):
        started, stopped = asyncio.Event(), asyncio.Event()
        async def loader():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        task = asyncio.create_task(self.service.generate(self.key, "watercolor", reference_loader=loader))
        await asyncio.wait_for(started.wait(), 3)
        await self.service.cancel(self.key)
        with self.assertRaisesRegex(PluginError, "取消"):
            await task
        self.assertTrue(stopped.is_set())
        self.assertFalse(self.service.sessions)

    async def test_model_catalog_pagination_hides_and_deduplicates(self):
        self.mode = "models_paginated"
        models = await self.service.list_models(self.key)
        self.assertEqual([model["model"] for model in models], ["model-a", "model-b"])
        self.assertIn("image", models[0]["input_modalities"])
        self.assertNotIn("image", models[1]["input_modalities"])

    async def test_model_catalog_requires_login_and_rejects_cursor_loop(self):
        self.mode = "anonymous"
        with self.assertRaisesRegex(PluginError, "登录"):
            await self.service.list_models(self.key)
        self.mode = "models_repeat"
        with self.assertRaisesRegex(PluginError, "分页"):
            await self.service.list_models(self.key)

    async def test_quota_reads_live_snapshot_without_credentials_or_inference(self):
        data = await self.service.read_quota(self.key)
        self.assertEqual(data["buckets"][0]["windows"][0]["remaining_percent"], 75)
        self.assertEqual(data["buckets"][0]["windows"][1]["remaining_percent"], 20)
        self.assertNotIn("secret", str(data))
        self.assertFalse(self.service.sessions)
        self.mode = "quota_empty"
        self.assertEqual((await self.service.read_quota(self.key))["buckets"], [])

    async def test_quota_login_protocol_errors_and_concurrency(self):
        for mode, message in [("anonymous", "登录"), ("quota_unsupported", "不支持额度查询"), ("quota_error", "失败")]:
            with self.subTest(mode=mode):
                self.mode = mode
                with self.assertRaisesRegex(PluginError, message) as raised:
                    await self.service.read_quota(self.key)
                self.assertNotIn("secret", str(raised.exception))
                self.assertFalse(self.service.sessions)
        self.mode = "success"
        async with self.service.session(self.key):
            with self.assertRaisesRegex(PluginError, "已有"):
                await self.service.read_quota(self.key)


class BoundaryTests(unittest.TestCase):
    def test_identity_is_platform_scoped_and_path_safe(self):
        self.assertNotEqual(user_key("qq1", "1"), user_key("qq2", "1"))
        self.assertNotEqual(user_key("a:b", "c"), user_key("a", "b:c"))
        self.assertEqual(len(user_key("qq1", "../../auth.json")), 64)
        with self.assertRaises(PluginError):
            user_key("", "1")

    def test_subprocess_does_not_inherit_credentials(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret", "CODEX_HOME": "host", "CODEX_THREAD_ID": "host-thread"}):
            env = child_environment(Path("isolated"))
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("CODEX_THREAD_ID", env)
            self.assertEqual(env["CODEX_HOME"], "isolated")

    def test_image_boundaries_and_format(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home, job = root / "codex", root / "job"
            job.mkdir()
            other = root / "other.png"
            other.write_bytes(PNG)
            for item in [
                {"savedPath": str(other)},
                {"savedPath": str(home / "auth.json")},
                {"result": base64.b64encode(b"not an image").decode()},
                {"result": "@bad-base64"},
                {"result": base64.b64encode(PNG * 100).decode()},
            ]:
                with self.subTest(item=item), self.assertRaises(PluginError):
                    save_image(item, home, job, job, 100)
            good = {"result": "data:image/png;base64," + base64.b64encode(PNG).decode()}
            self.assertEqual(save_image(good, home, job, job, 100).read_bytes(), PNG)

    def test_config_validation(self):
        for config in [{"max_sessions": 0}, {"generation_timeout": -1}, {"max_image_mb": 100}, {"request_timeout": True}]:
            with self.assertRaises(ValueError):
                Settings.from_config(config)


if __name__ == "__main__":
    unittest.main()
