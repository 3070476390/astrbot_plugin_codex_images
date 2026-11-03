"""Normalize public quota fields without exposing account identifiers or raw replies."""
import math
import re
import time
from datetime import datetime, timedelta, timezone

from .errors import PluginError

QUOTA_NOTE = "这是 Codex 账号额度，不能换算为剩余图片张数；实际可用性以服务返回为准。"
REACHED = {
    "rate_limit_reached": "已达到用量限制",
    "workspace_owner_credits_depleted": "工作区额度已耗尽",
    "workspace_member_credits_depleted": "成员额度已耗尽",
    "workspace_owner_usage_limit_reached": "工作区已达到用量限制",
    "workspace_member_usage_limit_reached": "成员已达到用量限制",
}


def _label(value):
    return " ".join("".join(c for c in value if c.isprintable() or c.isspace()).split())[:100] if isinstance(value, str) else ""


def _integer(value, low=0, high=2147483647):
    return value if type(value) is int and low <= value <= high else None


def _timestamp(value):
    return _integer(value, high=253402214399)


def _window(value, fallback):
    if not isinstance(value, dict):
        return None
    used = value.get("usedPercent")
    if type(used) not in (int, float) or not 0 <= used <= 2147483647 or not math.isfinite(used):
        used = None
    duration = _integer(value.get("windowDurationMins"), low=1)
    label = fallback
    if duration:
        if duration % 1440 == 0:
            label = f"{duration // 1440} 天窗口"
        elif duration % 60 == 0:
            label = f"{duration // 60} 小时窗口"
        else:
            label = f"{duration} 分钟窗口"
    return {"label": label, "used_percent": used,
            "remaining_percent": max(0, 100 - used) if used is not None else None,
            "resets_at": _timestamp(value.get("resetsAt"))}


def normalize_quota(raw):
    if not isinstance(raw, dict):
        raise PluginError("Codex 未返回有效的额度信息，请检查 CLI 版本后重试。")
    snapshots = {}
    multiple = raw.get("rateLimitsByLimitId")
    if isinstance(multiple, dict):
        snapshots.update((key, value) for key, value in multiple.items() if isinstance(value, dict) and value)
    legacy = raw.get("rateLimits")
    if isinstance(legacy, dict) and legacy:
        key = legacy.get("limitId") or "codex"
        if isinstance(key, str):
            snapshots.setdefault(key, legacy)
    buckets = []
    for key, value in list(snapshots.items())[:20]:
        windows = [_window(value.get(name), label) for name, label in (("primary", "主要窗口"), ("secondary", "次要窗口"))]
        credits = value.get("credits")
        balance = credits.get("balance") if isinstance(credits, dict) else None
        if not isinstance(balance, str) or not re.fullmatch(r"[0-9]{1,20}(?:\.[0-9]{1,10})?", balance):
            balance = None
        buckets.append({
            "name": _label(value.get("limitName")) or _label(key) or "Codex",
            "plan": _label(value.get("planType")),
            "windows": [window for window in windows if window is not None],
            "credits": {"balance": balance,
                        "has_credits": credits.get("hasCredits") if type(credits.get("hasCredits")) is bool else None,
                        "unlimited": credits.get("unlimited") is True} if isinstance(credits, dict) else None,
            "limit_message": REACHED.get(value.get("rateLimitReachedType")) if isinstance(value.get("rateLimitReachedType"), str) else None,
            "spend_control_reached": value.get("spendControlReached") is True,
        })
    resets = raw.get("rateLimitResetCredits")
    return {"buckets": buckets, "fetched_at": int(time.time()), "truncated": len(snapshots) > 20,
            "ordinary_usage_allowed": raw.get("ordinaryUsageAllowed") if type(raw.get("ordinaryUsageAllowed")) is bool else None,
            "reset_credits": _integer(resets.get("availableCount")) if isinstance(resets, dict) else None}


def _time_label(timestamp):
    if timestamp is None:
        return "未提供"
    return datetime.fromtimestamp(timestamp, timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def format_quota(data, scope):
    lines = [f"Codex 额度 · {scope}", f"查询时间：{_time_label(data['fetched_at'])}（UTC+8）"]
    if data["ordinary_usage_allowed"] is False:
        lines.append("服务当前不允许使用订阅内额度。")
    if not data["buckets"]:
        lines.append("服务暂未提供额度数据，不代表额度为零。")
    for bucket in data["buckets"][:6]:
        lines.append(f"\n{bucket['name']}" + (f" · {bucket['plan']}" if bucket["plan"] else ""))
        if not bucket["windows"]:
            lines.append("用量窗口：未提供")
        for window in bucket["windows"]:
            used, remaining = window["used_percent"], window["remaining_percent"]
            lines.append(f"{window['label']}：" + (f"剩余 {remaining:g}%（已用 {used:g}%）" if used is not None else "用量未提供"))
            lines.append(f"重置时间：{_time_label(window['resets_at'])}" + ("（UTC+8）" if window["resets_at"] is not None else ""))
        credits = bucket["credits"]
        if credits:
            balance = "不限额" if credits["unlimited"] else credits["balance"]
            if balance is None:
                balance = {True: "有可用额度，数值未提供", False: "无可用额度", None: "未提供"}[credits["has_credits"]]
            lines.append(f"Credits 余额：{balance}")
        if bucket["limit_message"]:
            lines.append(bucket["limit_message"])
        if bucket["spend_control_reached"]:
            lines.append("已达到支出控制限制。")
    if len(data["buckets"]) > 6 or data["truncated"]:
        lines.append("聊天消息仅展示前 6 个额度分组。")
    if data["reset_credits"] is not None:
        lines.append(f"\n可用额度重置次数：{data['reset_credits']}（仅展示）")
    lines.append("\n" + QUOTA_NOTE)
    return "\n".join(lines)
