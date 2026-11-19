import base64
import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import httpx
from PIL import Image

from codex_images.errors import PluginError
from codex_images.openai_images import OpenAIImages
from codex_images.service import ImageService, Settings, user_key


class ApiImagesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.key = user_key("mock-platform", "mock-user")
        self.settings = Settings.from_config({"openai_api_key": "test-only-key"})
        self.service = ImageService(Path(self.temp.name), self.settings)
        self.addAsyncCleanup(self.service.close)
        buffer = BytesIO()
        with Image.new("RGB", (160, 120), "blue") as img:
            img.save(buffer, format="PNG")
        self.png = buffer.getvalue()

    def mock_api(self, handler):
        def client(**options):
            self.assertFalse(options["follow_redirects"])
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), **options)
        self.service.api = OpenAIImages(client)

    async def generate(self, **options):
        return await self.service.generate_api(
            self.key, "夜景", model=self.settings.openai_image_model, quality="medium", **options,
        )

    async def test_generation_calls_images_api_and_returns_exact_ratio(self):
        async def handler(request):
            self.assertEqual(str(request.url), "https://api.openai.com/v1/images/generations")
            self.assertEqual(request.headers["Authorization"], "Bearer test-only-key")
            data = json.loads(await request.aread())
            self.assertEqual(data["prompt"], "夜景")
            self.assertEqual(data["model"], self.settings.openai_image_model)
            self.assertEqual(data["quality"], "medium")
            self.assertEqual(data["size"], "1024x1536")
            self.assertEqual(data["safety_identifier"], self.key)
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(self.png).decode()}]})

        self.mock_api(handler)
        result, = await self.generate(ratio="9:16")
        with Image.open(result) as image:
            self.assertEqual(image.size, (63, 112))
        self.assertFalse(self.service.sessions)

    async def test_missing_configured_key_never_uses_environment(self):
        self.settings.openai_api_key = ""
        self.mock_api(lambda request: self.fail("an API request was sent without a configured key"))
        with patch.dict("os.environ", {
            "ASTRBOT_CODEX_IMAGES_OPENAI_API_KEY": "unused-legacy-key",
            "OPENAI_API_KEY": "unused-global-key",
        }):
            with self.assertRaisesRegex(PluginError, "尚未配置"):
                await self.generate(ratio=None)

    async def test_edit_sends_reference_as_multipart_and_keeps_the_response(self):
        async def handler(request):
            self.assertEqual(str(request.url), "https://api.openai.com/v1/images/edits")
            self.assertIn("multipart/form-data", request.headers["content-type"])
            body = await request.aread()
            self.assertIn(b'name="image[]"; filename="reference.png"', body)
            self.assertIn(self.png, body)
            self.assertNotIn(b'name="size"', body)
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(self.png).decode()}]})

        async def load_reference():
            return self.png

        self.mock_api(handler)
        result, = await self.generate(ratio=None, reference_loader=load_reference)
        self.assertEqual(result.read_bytes(), self.png)

    async def test_provider_errors_do_not_expose_private_details_or_create_files(self):
        for status, response, expected in (
            (401, {"error": {"message": "private-provider-detail"}}, "认证失败"),
            (429, {"error": {"message": "private-provider-detail"}}, "速率或用量限制"),
            (200, {"data": [{"url": "https://example.invalid/private-provider-detail"}]}, "没有返回"),
        ):
            with self.subTest(status=status):
                self.mock_api(lambda request: httpx.Response(status, json=response))
                with self.assertRaisesRegex(PluginError, expected) as raised:
                    await self.generate(ratio=None)
                self.assertNotIn("private-provider-detail", str(raised.exception))
                self.assertEqual(self.service.active_tasks, 0)
                self.assertFalse((self.service.root / "api_jobs").exists())
