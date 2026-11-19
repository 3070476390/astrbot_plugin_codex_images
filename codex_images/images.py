import base64
import binascii
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageOps

from .errors import PluginError

MAX_CROP_PIXELS = 16_000_000


def image_suffix(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    raise PluginError("Codex 返回的内容不是支持的 PNG、JPEG 或 WebP 图片。")


def crop_to_ratio(data: bytes, ratio: tuple[int, int], max_bytes: int) -> bytes:
    """Center crop without stretching, retaining the input format and transparency."""
    horizontal, vertical = ratio
    try:
        with Image.open(BytesIO(data), formats=("PNG", "JPEG", "WEBP")) as source:
            width, height = source.size
            if width * height > MAX_CROP_PIXELS:
                raise PluginError("生成的图片像素过多，无法裁切到指定比例。")
            if source.getexif().get(274) in (5, 6, 7, 8):
                width, height = height, width
            if width * vertical == height * horizontal:
                source.load()
                return data
            scale = min(width // horizontal, height // vertical)
            if not scale:
                raise PluginError("生成的图片尺寸过小，无法裁切到指定比例。")
            target_width, target_height = scale * horizontal, scale * vertical
            left = (width - target_width) // 2
            top = (height - target_height) // 2
            with ImageOps.exif_transpose(source) as oriented:
                with oriented.crop((left, top, left + target_width, top + target_height)) as cropped:
                    result = BytesIO()
                    options = {"quality": 95} if source.format in ("JPEG", "WEBP") else {}
                    cropped.save(result, format=source.format, **options)
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise PluginError("无法处理生成的图片，未能裁切到指定比例。") from exc
    if result.tell() > max_bytes:
        raise PluginError("裁切后的图片超过大小限制。")
    return result.getvalue()


def save_image(item: dict, home: Path, workspace: Path, output: Path, max_bytes: int) -> Path:
    """Accept only image events; never scrape arbitrary paths from model text."""
    if item.get("failure"):
        raise PluginError("Codex 图像生成失败，可能已达到账号使用限额。请稍后重试。")
    result = item.get("result")
    if result:
        if not isinstance(result, str) or len(result) > (max_bytes * 4 // 3 + 128):
            raise PluginError("返回的图片超过大小限制。")
        if result.startswith("data:image/"):
            header, separator, result = result.partition(",")
            if not separator or not header.endswith(";base64"):
                raise PluginError("图片编码格式不正确。")
        try:
            data = base64.b64decode(result, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise PluginError("图片编码格式不正确。") from exc
    elif item.get("savedPath"):
        path = Path(item["savedPath"]).resolve()
        # Resolve symlinks before checking; never accept another user's directory.
        roots = [workspace.resolve(), (home / "generated_images").resolve()]
        if not any(path.is_relative_to(root) for root in roots):
            raise PluginError("Codex 图片路径不在当前用户的输出目录中。")
        try:
            with path.open("rb") as stream:
                data = stream.read(max_bytes + 1)
        except OSError as exc:
            raise PluginError("无法读取 Codex 生成的图片文件。") from exc
    else:
        raise PluginError("当前 Codex 只返回了图片引用，未提供图片数据或本地文件。请更换兼容的 CLI 版本。")
    if len(data) > max_bytes:
        raise PluginError("返回的图片超过大小限制。")
    suffix = image_suffix(data)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = output / (uuid4().hex + suffix)
    with target.open("xb") as stream:
        stream.write(data)
    target.chmod(0o600)
    return target
