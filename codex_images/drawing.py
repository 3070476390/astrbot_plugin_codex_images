import asyncio
import re
from dataclasses import dataclass

from .errors import PluginError

RATIOS = {"1:1": (1, 1), "9:16": (9, 16), "16:9": (16, 9)}
RATIO_HELP = "出图比例支持 1:1、9:16、16:9，例如 --比例 9:16。"
RATIO_OPTION = re.compile(r"^--(?:比例|ratio)(?:=|\s+|$)")
DRAWING_ID = re.compile(r"[0-9a-f]{12}")
REDRAW_LABEL = re.compile(r"/codex重绘[ \t]+(" + DRAWING_ID.pattern + r")(?!\w)")


def parse_drawing_prompt(text: str) -> tuple[str, str | None]:
    option = RATIO_OPTION.match(text)
    if option is None:
        return text, None
    parts = text[option.end():].split(maxsplit=1)
    ratio = parts[0].replace("：", ":") if parts else ""
    if ratio not in RATIOS:
        raise PluginError(RATIO_HELP)
    prompt = parts[1] if len(parts) == 2 else ""
    if RATIO_OPTION.match(prompt):
        raise PluginError("每次只能指定一个出图比例。")
    return prompt, ratio


@dataclass(frozen=True)
class DrawingRequest:
    prompt: str
    ratio: str | None
    model: str
    reference: bytes | None
    quality: str | None = None

    @property
    def reference_size(self) -> int:
        return len(self.reference) if self.reference is not None else 0


@dataclass
class RedrawRecord:
    scope: tuple[str, str, str]
    drawing: DrawingRequest
    expiry: asyncio.TimerHandle


class RedrawHistory:
    lifetime = 30 * 60
    max_records = 128
    max_reference_bytes = 64 * 1024 * 1024

    def __init__(self):
        self.records: dict[str, RedrawRecord] = {}

    def remember(self, drawing_id, scope, drawing: DrawingRequest):
        self.discard(drawing_id)
        if drawing.reference_size > self.max_reference_bytes:
            return
        reference_bytes = drawing.reference_size + sum(
            entry.drawing.reference_size for entry in self.records.values()
        )
        while len(self.records) >= self.max_records or reference_bytes > self.max_reference_bytes:
            oldest_id = next(iter(self.records))
            reference_bytes -= self.records[oldest_id].drawing.reference_size
            self.discard(oldest_id)
        timer = asyncio.get_running_loop().call_later(self.lifetime, self.discard, drawing_id)
        self.records[drawing_id] = RedrawRecord(scope, drawing, timer)

    def get(self, drawing_id, scope) -> DrawingRequest:
        entry = self.records.get(drawing_id)
        if entry is None or entry.scope != scope:
            raise PluginError("找不到可重绘的记录。请使用当前会话中自己的近期重绘编号，并切到原出图模式。")
        return entry.drawing

    def discard(self, drawing_id):
        entry = self.records.pop(drawing_id, None)
        if entry is not None:
            entry.expiry.cancel()

    def clear(self):
        for drawing_id in tuple(self.records):
            self.discard(drawing_id)
