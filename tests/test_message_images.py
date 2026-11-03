from contextlib import asynccontextmanager
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from test_plugin import Image, load_plugin

PNG = b"\x89PNG\r\n\x1a\nfixture"


class ReferenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.plugin = load_plugin(self.temp.name)
        self.media = sys.modules["plugin_under_test.codex_images.message_images"]

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    async def test_adapter_resolver_and_cleanup_lifecycle(self):
        source = Path(self.temp.name) / "adapter.png"
        source.write_bytes(PNG)
        cleaned = []
        class Resolver:
            def __init__(self, ref, **kwargs):
                self.ref = ref
            @asynccontextmanager
            async def as_path(self):
                try:
                    yield types.SimpleNamespace(path=source)
                finally:
                    cleaned.append(True)
        module = types.ModuleType("astrbot.core.utils.media_utils")
        module.MediaResolver = Resolver
        with patch.dict(sys.modules, {"astrbot.core.utils.media_utils": module}):
            self.assertEqual(await self.media.load_reference(Image(file=str(source)), 100, 2), PNG)
            source.write_bytes(PNG * 100)
            with self.assertRaises(Exception):
                await self.media.load_reference(Image(file=str(source)), 100, 2)
        self.assertEqual(len(cleaned), 2)
        self.assertTrue(source.exists(), "adapter-owned source must not be deleted by plugin")
