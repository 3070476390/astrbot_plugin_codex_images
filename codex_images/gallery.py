import base64
import re
import sqlite3
import time
from contextlib import closing, contextmanager
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageOps

from .errors import PluginError
from .images import MAX_CROP_PIXELS, image_suffix

GALLERY_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}
GALLERY_ID = re.compile(r"[0-9a-f]{32}")


def _preview(data: bytes, side: int) -> tuple[bytes, int, int]:
    try:
        with Image.open(BytesIO(data), formats=("PNG", "JPEG", "WEBP")) as image:
            width, height = image.size
            if width * height > MAX_CROP_PIXELS:
                return b"", width, height
            ImageOps.exif_transpose(image, in_place=True)
            width, height = image.size
            image.thumbnail((side, side), Image.Resampling.LANCZOS)
            output = BytesIO()
            image.save(output, format="WEBP", quality=80)
            return output.getvalue(), width, height
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise PluginError("无法创建图片预览。") from exc


class Gallery:
    retention_days = 7
    max_items = 120
    max_bytes = 256 * 1024 * 1024
    page_size = 12

    def __init__(self, root: Path):
        self.database = root / "images.sqlite3"

    @property
    def cutoff(self) -> float:
        return time.time() - self.retention_days * 86400

    @contextmanager
    def _connect(self):
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(self.database, timeout=10)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA auto_vacuum = FULL")
            db.execute("PRAGMA secure_delete = ON")
            db.execute("""
                CREATE TABLE IF NOT EXISTS gallery (
                    id TEXT PRIMARY KEY, created REAL NOT NULL, backend TEXT NOT NULL,
                    model TEXT NOT NULL, prompt TEXT NOT NULL, extension TEXT NOT NULL,
                    width INTEGER NOT NULL, height INTEGER NOT NULL, size INTEGER NOT NULL,
                    original BLOB NOT NULL, thumbnail BLOB NOT NULL
                )
            """)
            yield db

    def save(self, path: Path, *, backend: str, model: str, prompt: str, max_image_bytes: int):
        with path.open("rb") as image:
            data = image.read(max_image_bytes + 1)
        if len(data) > max_image_bytes:
            raise PluginError("图片超过图库的单张大小限制。")
        extension = image_suffix(data)
        thumbnail, width, height = _preview(data, 384)
        size = len(data) + len(thumbnail)
        if size > self.max_bytes:
            raise PluginError("图片超过图库的容量限制。")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM gallery WHERE created <= ?", (self.cutoff,))
            db.execute(
                "INSERT INTO gallery VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (uuid4().hex, time.time(), backend, model, prompt, extension,
                 width, height, size, data, thumbnail),
            )
            entries = db.execute("SELECT id, size FROM gallery ORDER BY created DESC, rowid DESC").fetchall()
            bytes_used = 0
            for index, entry in enumerate(entries):
                bytes_used += entry["size"]
                if index >= self.max_items or bytes_used > self.max_bytes:
                    db.executemany("DELETE FROM gallery WHERE id = ?", ((old["id"],) for old in entries[index:]))
                    break

    def prune(self):
        if self.database.is_file():
            with self._connect() as db:
                db.execute("DELETE FROM gallery WHERE created <= ?", (self.cutoff,))

    def list(self, page: int, backend: str, search: str):
        if backend not in ("", "codex", "api"):
            raise PluginError("不支持的图库筛选条件。")
        query = search.strip()[:200].lower()
        total, pages, rows = 0, 1, []
        if self.database.is_file():
            with self._connect() as db:
                db.execute("BEGIN")
                where = """WHERE created > ? AND (? = '' OR backend = ?)
                           AND (instr(lower(prompt), ?) OR instr(lower(model), ?) OR instr(id, ?))"""
                params = (self.cutoff, backend, backend, query, query, query)
                total = db.execute(f"SELECT COUNT(*) FROM gallery {where}", params).fetchone()[0]
                pages = max(1, (total + self.page_size - 1) // self.page_size)
                page = min(max(1, page), pages)
                rows = db.execute(
                    f"""SELECT id, created, backend, model, prompt, width, height, thumbnail
                        FROM gallery {where} ORDER BY created DESC, rowid DESC LIMIT ? OFFSET ?""",
                    (*params, self.page_size, (page - 1) * self.page_size),
                ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["thumbnail"] = base64.b64encode(item["thumbnail"]).decode("ascii")
            items.append(item)
        return {"items": items, "page": min(max(1, page), pages), "pages": pages, "total": total}

    def original(self, item_id: str) -> tuple[bytes, str]:
        if GALLERY_ID.fullmatch(item_id) and self.database.is_file():
            with self._connect() as db:
                row = db.execute(
                    "SELECT original, extension FROM gallery WHERE id = ? AND created > ?",
                    (item_id, self.cutoff),
                ).fetchone()
            if row:
                return row["original"], row["extension"]
        raise PluginError("这张图片已过期或不在图库中。")

    def preview(self, item_id: str) -> dict:
        data, _ = self.original(item_id)
        preview, _, _ = _preview(data, 1440)
        if not preview:
            raise PluginError("图片像素较多，可下载原图查看。")
        return {"image": base64.b64encode(preview).decode("ascii")}
