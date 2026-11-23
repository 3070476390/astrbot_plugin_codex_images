from .errors import PluginError

MODELS_PER_PAGE = 10
MODEL_COMMAND_USAGE = "发送 /codex模型 查看列表，/codex模型 页 2 翻页，或 /codex模型 编号 切换。"


def parse_model_action(message: str) -> tuple[str, int]:
    args = message.split()[1:]
    if not args:
        return "list", 1
    if args == ["默认"]:
        return "reset", 0
    if len(args) == 2 and args[0].lower() in {"页", "page"}:
        action, number = "page", args[1]
    elif len(args) == 1:
        action, number = "select", args[0]
    else:
        raise PluginError(MODEL_COMMAND_USAGE)
    if len(number) > 6 or not number.isascii() or not number.isdecimal() or int(number) < 1:
        raise PluginError(MODEL_COMMAND_USAGE)
    return action, int(number)


def model_label(model: str) -> str:
    return " ".join(model.split())[:100]


def format_model_catalog(models: list[dict], page: int, active: str, *, use_api: bool) -> str:
    pages = (len(models) + MODELS_PER_PAGE - 1) // MODELS_PER_PAGE
    if page > pages:
        raise PluginError(f"模型列表只有 {pages} 页。")
    if not active and not use_api:
        active = next((item["model"] for item in models if item["is_default"]), "")
    title = "OpenAI API 图像模型" if use_api else "Codex 调度模型"
    lines = [f"{title}（{page}/{pages} 页，共 {len(models)} 个）"]
    start = (page - 1) * MODELS_PER_PAGE
    page_models = models[start:start + MODELS_PER_PAGE]
    for number, item in enumerate(page_models, start + 1):
        model = item["model"]
        line = f"{number}. {model_label(model)}"
        if not use_api:
            flags = ["默认"] if item["is_default"] else []
            flags.append("图片输入" if "image" in item["input_modalities"] else "无图片输入")
            line += f" · {' / '.join(flags)}"
        if model == active:
            line += " ✓"
        lines.append(line)
    if active and not any(item["model"] == active for item in page_models):
        active_number = next((number for number, item in enumerate(models, 1) if item["model"] == active), None)
        if active_number is None:
            lines.append(f"当前选择 {model_label(active)}（目录未返回）")
        else:
            lines.append(f"当前模型 {active_number}. {model_label(active)} ✓")
    elif not active:
        lines.append("当前使用 Codex 默认模型。")
    if page < pages:
        lines.append(f"下一页 /codex模型 页 {page + 1}")
    if page > 1:
        lines.append(f"上一页 /codex模型 页 {page - 1}")
    lines.append("发送 /codex模型 编号 切换，或 /codex模型 默认 恢复管理员设置。")
    lines.append("实际出图权限以调用结果为准。")
    return "\n".join(lines)
