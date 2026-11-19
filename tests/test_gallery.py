import base64
import sqlite3
import tempfile
import unittest
from contextlib import closing
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from codex_images.errors import PluginError
from codex_images.gallery import Gallery


class GalleryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "image.png"
        with Image.new("RGBA", (600, 1200), (20, 40, 80, 100)) as image:
            image.save(self.path)
        self.gallery = Gallery(self.root / "gallery")

    def save(self, prompt, backend="codex"):
        self.gallery.save(
            self.path, backend=backend, model="GPT-TEST",
            prompt=prompt, max_image_bytes=64 * 1024,
        )

    def test_archive_survives_restart_and_supports_filters_and_previews(self):
        self.gallery.page_size = 1
        self.save("蓝色测试")
        self.save("Second prompt", "api")
        first_page = self.gallery.list(1, "", "")
        self.assertEqual((first_page["total"], first_page["pages"]), (2, 2))
        self.assertEqual(first_page["items"][0]["backend"], "api")
        self.assertEqual(self.gallery.list(2, "", "")["items"][0]["prompt"], "蓝色测试")
        self.assertEqual(self.gallery.list(1, "codex", "second")["total"], 0)
        item = Gallery(self.root / "gallery").list(1, "api", "gpt-test")["items"][0]
        self.assertEqual((item["width"], item["height"]), (600, 1200))
        with Image.open(BytesIO(base64.b64decode(item["thumbnail"]))) as thumbnail:
            self.assertEqual(thumbnail.size, (192, 384))
        with Image.open(BytesIO(base64.b64decode(self.gallery.preview(item["id"])["image"]))) as preview:
            self.assertEqual(preview.size, (600, 1200))
        self.assertEqual(self.gallery.original(item["id"]), (self.path.read_bytes(), ".png"))

    def test_expiry_blocks_reads_and_prune_removes_stored_data(self):
        with patch("codex_images.gallery.time.time", return_value=2_000_000):
            self.save("expire")
            item_id = self.gallery.list(1, "", "")["items"][0]["id"]
        with patch("codex_images.gallery.time.time", return_value=2_000_000 + 7 * 86400):
            self.assertEqual(self.gallery.list(1, "", "")["total"], 0)
            with self.assertRaisesRegex(PluginError, "过期"):
                self.gallery.original(item_id)
            self.gallery.prune()
        with closing(sqlite3.connect(self.gallery.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gallery").fetchone()[0], 0)

    def test_large_image_with_no_preview_is_still_available_as_original(self):
        with patch("codex_images.gallery.MAX_CROP_PIXELS", 100):
            self.save("large")
            item = self.gallery.list(1, "", "")["items"][0]
            self.assertFalse(item["thumbnail"])
            self.assertEqual(self.gallery.original(item["id"])[0], self.path.read_bytes())
            with self.assertRaisesRegex(PluginError, "下载原图"):
                self.gallery.preview(item["id"])

    def test_gallery_discards_oldest_entries_when_count_or_bytes_are_exceeded(self):
        self.gallery.max_items = 2
        for prompt in ("first", "second", "third"):
            self.save(prompt)
        self.assertEqual([row["prompt"] for row in self.gallery.list(1, "", "")["items"]], ["third", "second"])
        with closing(sqlite3.connect(self.gallery.database)) as db:
            self.gallery.max_bytes = db.execute("SELECT MAX(size) FROM gallery").fetchone()[0]
        self.save("fourth")
        self.assertEqual([row["prompt"] for row in self.gallery.list(1, "", "")["items"]], ["fourth"])
