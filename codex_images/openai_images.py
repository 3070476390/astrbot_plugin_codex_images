import asyncio
import json
import os

import httpx

from .drawing import RATIOS
from .errors import PluginError
from .images import crop_to_ratio, decode_image, image_suffix

API_JOB_KEY = "openai_api"
API_KEY_ENV = "ASTRBOT_CODEX_IMAGES_OPENAI_API_KEY"
API_DEFAULT_MODEL = "gpt-image-2.6-flare"
API_SIZES = {"1:1": "1024x1024", "9:16": "1024x1536", "16:9": "1536x1024"}
MIME_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}
API_ERRORS = {
    400: "OpenAI API 拒绝了绘图请求，请检查提示词、参考图或模型设置。",
    401: "OpenAI API 认证失败，请联系管理员检查密钥。",
    403: "OpenAI API 项目没有此模型或操作的权限。",
    404: "OpenAI API 找不到此图像模型，请联系管理员检查模型设置。",
    429: "OpenAI API 达到速率或用量限制，请稍后重试或联系管理员检查项目用量。",
}


def api_key() -> str:
    return os.environ.get(API_KEY_ENV, "").strip()


class OpenAIImages:
    def __init__(self, client_factory=httpx.AsyncClient):
        self.client_factory = client_factory

    async def generate(self, settings, caller, prompt, *, ratio, model, quality, reference_loader):
        key = api_key()
        if not key:
            raise PluginError("服务器尚未配置 OpenAI API Key，请联系管理员。")
        max_bytes = settings.max_image_bytes
        try:
            async with asyncio.timeout(settings.generation_timeout):
                reference = await reference_loader() if reference_loader is not None else None
                fields = {"model": model, "prompt": prompt, "quality": quality,
                          "output_format": "png", "safety_identifier": caller}
                if ratio is not None:
                    fields["size"] = API_SIZES[ratio]
                data = await self._request(key, fields, reference, settings)
                if ratio is not None:
                    data = await asyncio.to_thread(crop_to_ratio, data, RATIOS[ratio], max_bytes)
        except (TimeoutError, httpx.TimeoutException):
            raise PluginError("OpenAI API 绘图超时，已提交的请求可能计费。") from None
        except httpx.RequestError:
            raise PluginError("无法完成 OpenAI API 请求，请检查网络，已提交的请求可能计费。") from None

        return data

    async def _request(self, key, fields, reference, settings):
        max_bytes = settings.max_image_bytes
        if reference is None:
            route = "generations"
            body = {"json": fields}
        else:
            if not isinstance(reference, bytes) or len(reference) > max_bytes:
                raise PluginError("参考图不可读取或超过图片大小限制。")
            suffix = image_suffix(reference)
            route = "edits"
            body = {"data": fields, "files": {"image[]": ("reference" + suffix, reference, MIME_TYPES[suffix])}}

        timeout = httpx.Timeout(settings.generation_timeout, connect=settings.request_timeout)
        async with self.client_factory(timeout=timeout, follow_redirects=False) as client:
            async with client.stream(
                "POST", f"https://api.openai.com/v1/images/{route}",
                headers={"Authorization": f"Bearer {key}"}, **body,
            ) as response:
                if not response.is_success:
                    raise PluginError(API_ERRORS.get(response.status_code, "OpenAI API 绘图失败，请稍后重试。"))
                raw = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                    raw.extend(chunk)
                    if len(raw) > max_bytes * 4 // 3 + 64 * 1024:
                        raise PluginError("OpenAI API 返回的数据超过图片大小限制。")
        try:
            encoded = json.loads(raw)["data"][0]["b64_json"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise PluginError("OpenAI API 没有返回可用的图片数据。") from None
        return decode_image(encoded, max_bytes)
