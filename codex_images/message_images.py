"""Resolve only images supplied by the message adapter, including quoted images."""
import asyncio
from pathlib import Path

from astrbot.api.message_components import Image, Reply

from .errors import PluginError
from .images import image_suffix


def reference_from_message(event):
    chain = event.get_messages()
    images = [item for item in chain if isinstance(item, Image)]
    if not images:
        for item in chain:
            if isinstance(item, Reply):
                images.extend(part for part in (item.chain or []) if isinstance(part, Image))
    if len(images) > 1:
        raise PluginError("图生图每次支持一张参考图，请只附带或引用一张图片。")
    return images[0] if images else None


async def load_reference(image, max_bytes: int, timeout: float) -> bytes:
    # Use AstrBot's media resolver so adapter-specific cached/local/base64/URL
    # inputs share its lifecycle. Never interpret prompt text as a path or URL.
    from astrbot.core.utils.media_utils import MediaResolver

    source = image.url or image.file
    if not isinstance(source, str) or not source:
        raise PluginError("参考图不可读取，请重新上传图片。")
    if source.startswith(("base64://", "data:")) and len(source) > max_bytes * 4 // 3 + 256:
        raise PluginError("参考图超过图片大小限制。")
    try:
        async with asyncio.timeout(timeout):
            async with MediaResolver(source, media_type="image").as_path() as resolved:
                path = Path(resolved.path)
                if not path.is_file() or path.stat().st_size > max_bytes:
                    raise PluginError("参考图不可读取或超过图片大小限制。")
                # Bounded read, including when a cached file changes during load.
                with path.open("rb") as stream:
                    data = stream.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise PluginError("参考图超过图片大小限制。")
                image_suffix(data)
                return data
    except asyncio.TimeoutError as exc:
        raise PluginError("参考图读取超时，请重新上传图片后重试。") from exc
    except PluginError:
        raise
    except Exception as exc:
        raise PluginError("无法读取参考图，图片可能已过期，请重新上传或将图片与指令一起发送。") from exc
