from pathlib import Path
from uuid import uuid4

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .codex_images.errors import PluginError
from .codex_images.service import ImageService, Settings, user_key
from .codex_images.dashboard import DashboardController, SHARED_KEY
from .codex_images.drawing import DRAWING_ID, DrawingRequest, RedrawHistory, parse_drawing_prompt
from .codex_images.message_images import load_reference, quoted_drawing_id, reference_from_message
from .codex_images.openai_images import API_JOB_KEY, api_key
from .codex_images.quota import format_quota


class CodexImagesPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.image_owners = {}
        self.cancelled_images = set()
        self.redraw_history = RedrawHistory()
        self._plugin_context = context
        root = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_codex_images"
        self.service = ImageService(root, Settings.from_config(config))
        self.dashboard = DashboardController(self)

    async def initialize(self):
        from .codex_images.web_api import DashboardAPI
        self.web_api = DashboardAPI(self._plugin_context, self.dashboard)
        self.web_api.register()

    @property
    def account_mode(self):
        return self.config.get("account_mode", "shared")

    def _account_key(self, caller):
        return SHARED_KEY if self.account_mode == "shared" else caller

    async def cancel_image(self, key):
        reserved = key in self.image_owners
        if reserved:
            self.cancelled_images.add(key)
        stopped = await self.service.cancel_api() if key == API_JOB_KEY else await self.service.cancel(key)
        return reserved or stopped

    async def _backend(self, caller):
        stored = await self.get_kv_data(f"drawing_backend:{caller}", "codex")
        return "api" if stored == "api" else "codex"

    def _require_api(self):
        if self.config.get("openai_api_enabled") is not True:
            raise PluginError("管理员尚未启用 OpenAI API 绘图。可发送 /codex模式 Codex 使用订阅模式。")
        if not api_key():
            raise PluginError("服务器尚未配置 OpenAI API Key，请联系管理员。")

    def _check(self, event, private=False):
        if private and not event.is_private_chat():
            raise PluginError("账号操作请私聊机器人发送，设备码不会发送到群聊。")
        if not event.is_private_chat() and not self.config.get("allow_group_images", False):
            raise PluginError("当前仅允许私聊绘图；管理员可在插件配置中开启群聊绘图。")
        allowed = self.config.get("allowed_users", [])
        platform, sender = event.get_platform_id(), event.get_sender_id()
        if not platform or not sender:
            raise PluginError("无法识别当前平台或用户，未执行账号操作。")
        identity = f"{platform}:{sender}"
        if self.config.get("allow_all_users", False) is not True:
            if self.account_mode == "shared":
                if not event.is_admin() and identity not in allowed:
                    raise PluginError("你不在统一账号的使用名单中，请联系管理员添加 平台实例ID:用户ID。")
            elif allowed and identity not in allowed:
                raise PluginError("你不在插件使用名单中，请联系管理员。")
        return user_key(str(platform), str(sender))

    @filter.command("codex帮助")
    async def help_command(self, event: AstrMessageEvent):
        """查看 Codex 订阅绘图插件的使用方法。"""
        account_help = (
            "统一账号由管理员在 扩展功能 → Codex 绘图 中登录和退出。\n"
            if self.account_mode == "shared" else
            "/codex登录：私聊获取官方设备码并登录\n/codex退出：退出自己的账号\n"
        )
        yield event.plain_result(
            "Codex 绘图\n" + account_help +
            "/codex模式 [Codex|API]：查询或切换自己的绘图模式\n"
            "/codex状态：查询登录状态\n"
            "/codex画图 [--比例 9:16] 图片描述：生成一张图，支持 1:1、9:16、16:9\n"
            "/codex改图 [--比例 9:16] 修改要求：附带或引用一张图\n"
            "/codex重绘 [编号]：引用自己的出图结果，或输入重绘编号再生成一张\n"
            "/codex模型 [页码]：查看当前模式的模型\n"
            "/codex额度：查看当前模式的用量查询方式\n"
            "/codex取消：停止当前登录或绘图任务\n"
            "是否能生成图片取决于账号权限和可用额度。"
        )

    @filter.command("codex模式")
    async def mode_command(self, event: AstrMessageEvent):
        """查询或切换自己的绘图模式。"""
        try:
            caller = self._check(event)
            parts = event.get_message_str().split(maxsplit=1)
            selected = parts[1].strip().lower() if len(parts) == 2 else await self._backend(caller)
            if selected not in {"codex", "api"}:
                raise PluginError("用法：/codex模式、/codex模式 Codex 或 /codex模式 API")
            if len(parts) == 2:
                if selected == "api":
                    self._require_api()
                await self.put_kv_data(f"drawing_backend:{caller}", selected)
            if selected == "api":
                message = "当前绘图模式为 OpenAI API，绘图用量计入管理员配置的 API 项目。"
                try:
                    self._require_api()
                except PluginError as exc:
                    message += "\n" + str(exc)
            else:
                message = "当前绘图模式为 Codex，绘图使用 Codex 订阅额度。"
            yield event.plain_result(message)
        except PluginError as exc:
            yield event.plain_result(str(exc))
        except Exception as exc:
            logger.error("绘图模式设置异常 (%s)", type(exc).__name__)
            yield event.plain_result("无法读取或保存绘图模式，请稍后重试。")

    @filter.command("codex登录")
    async def login_command(self, event: AstrMessageEvent):
        """通过官方设备码登录自己的 Codex 订阅账号（仅私聊）。"""
        try:
            key = self._check(event, private=True)
            if self.account_mode == "shared":
                raise PluginError("当前使用统一绘图账号，请管理员在 扩展功能 → Codex 绘图 中登录。")
            async for step in self.service.login(key):
                if step["kind"] == "code":
                    yield event.plain_result(
                        f"请打开官方登录页面：{step['url']}\n设备码：{step['code']}\n"
                        "请在自己的浏览器中完成登录，不要把密码或令牌发送给机器人。\n"
                        "机器人所在服务器将保存登录凭据。完成后这里会通知你。"
                    )
                else:
                    yield event.plain_result("账号已登录。发送 /codex画图 图片描述 开始绘图。")
        except PluginError as exc:
            yield event.plain_result(str(exc))
        except Exception as exc:
            logger.error("Codex 登录异常 (%s)", type(exc).__name__)
            yield event.plain_result("登录遇到内部错误，请检查插件配置和 Codex 版本。")

    @filter.command("codex状态")
    async def status_command(self, event: AstrMessageEvent):
        """查看自己的登录状态（仅私聊）。"""
        try:
            caller = self._check(event, private=True)
            if await self._backend(caller) == "api":
                self._require_api()
                yield event.plain_result("当前使用 OpenAI API 模式，服务器已配置密钥。模型权限以实际请求为准。")
                return
            state = await self.service.status(self._account_key(caller))
            if state.get("busy"):
                message = "当前有任务正在进行，可发送 /codex取消 停止。"
            else:
                if state["logged_in"]:
                    message = "绘图账号已登录；图像能力以实际绘图结果为准。"
                else:
                    message = "统一账号尚未登录，请联系管理员在扩展页面登录。" if self.account_mode == "shared" else "尚未登录，请发送 /codex登录。"
            yield event.plain_result(message)
        except PluginError as exc:
            yield event.plain_result(str(exc))
        except Exception as exc:
            logger.error("Codex 状态异常 (%s)", type(exc).__name__)
            yield event.plain_result("无法查询状态，请检查 Codex 配置。")

    @filter.command("codex额度", alias={"codex用量"})
    async def quota_command(self, event: AstrMessageEvent):
        """查询当前绘图账号的额度快照。"""
        try:
            caller = self._check(event)
            if await self._backend(caller) == "api":
                yield event.plain_result("当前使用 OpenAI API 模式。请由管理员在 https://platform.openai.com/usage 查看项目用量。")
                return
            key = self._account_key(caller)
            if key in self.image_owners or (self.account_mode == "shared" and self.dashboard.login_pending):
                raise PluginError("绘图账号正在执行任务，请等待完成后查询额度。")
            data = await self.service.read_quota(key)
            yield event.plain_result(format_quota(data, "统一账号" if self.account_mode == "shared" else "我的账号"))
        except PluginError as exc:
            yield event.plain_result(str(exc))
        except Exception as exc:
            logger.error("Codex 额度查询异常 (%s)", type(exc).__name__)
            yield event.plain_result("无法查询额度，请检查 Codex 配置和网络。")

    @filter.command("codex退出")
    async def logout_command(self, event: AstrMessageEvent):
        """取消任务并清除自己的 Codex 登录凭据（仅私聊）。"""
        try:
            caller = self._check(event, private=True)
            if self.account_mode == "shared":
                raise PluginError("统一账号只能由管理员在 扩展功能 → Codex 绘图 中退出。")
            await self.service.logout(caller)
            yield event.plain_result("已退出账号。下次使用需要重新登录。")
        except PluginError as exc:
            yield event.plain_result(str(exc))
        except Exception as exc:
            logger.error("Codex 退出异常 (%s)", type(exc).__name__)
            yield event.plain_result("退出失败，请稍后重试。")

    @filter.command("codex取消")
    async def cancel_command(self, event: AstrMessageEvent):
        """停止自己的登录或绘图任务。"""
        try:
            caller = self._check(event)
            key = API_JOB_KEY if self.image_owners.get(API_JOB_KEY) == caller else self._account_key(caller)
            if key == SHARED_KEY and self.image_owners.get(key) != caller:
                raise PluginError("你当前没有正在运行的绘图任务。")
            cancelled = await self.cancel_image(key)
            yield event.plain_result("已停止当前任务。" if cancelled else "当前没有正在运行的任务。")
        except PluginError as exc:
            yield event.plain_result(str(exc))

    @filter.command("codex模型", alias={"codex模型列表"})
    async def models_command(self, event: AstrMessageEvent):
        """查询当前绘图账号可见的调度模型，可带页码。"""
        try:
            caller = self._check(event)
            parts = event.get_message_str().strip().split()
            if len(parts) > 2 or (len(parts) == 2 and (not parts[1].isascii() or not parts[1].isdigit() or len(parts[1]) > 6)):
                raise PluginError("用法：/codex模型 或 /codex模型 2")
            page = int(parts[1]) if len(parts) == 2 else 1
            if page < 1:
                raise PluginError("页码从 1 开始。")
            if await self._backend(caller) == "api":
                self._require_api()
                if page != 1:
                    raise PluginError("当前 API 图像模型只显示在第 1 页。")
                yield event.plain_result(f"当前 OpenAI API 图像模型为 {self.service.settings.openai_image_model}。")
                return
            key = self._account_key(caller)
            models = await self.service.list_models(key)
            if not models:
                raise PluginError("Codex 没有返回可见模型，请检查账号权限或 CLI 版本。")
            page_size = 10
            pages = (len(models) + page_size - 1) // page_size
            if page > pages:
                raise PluginError(f"模型列表只有 {pages} 页。")
            # Bound individual catalog strings so platform text limits are respected.
            def clean(value, limit=100):
                return " ".join(str(value).split())[:limit]
            lines = [f"当前账号可见模型（{page}/{pages} 页，共 {len(models)} 个）"]
            for model in models[(page - 1) * page_size:page * page_size]:
                flags = []
                if model["model"] == self.service.settings.model:
                    flags.append("当前设置")
                if model["is_default"]:
                    flags.append("默认")
                flags.append("图片输入" if "image" in model["input_modalities"] else "无图片输入")
                lines.append(f"• {clean(model['model'])} · {' / '.join(flags)}")
            lines.append("这是调度模型列表；支持图片输入不等于已开通图像生成权限。")
            if page < pages:
                lines.append(f"下一页：/codex模型 {page + 1}")
            yield event.plain_result("\n".join(lines))
        except PluginError as exc:
            yield event.plain_result(str(exc))
        except Exception as exc:
            logger.error("Codex 模型查询异常 (%s)", type(exc).__name__)
            yield event.plain_result("模型查询失败，请检查账号登录状态和 Codex 版本。")

    @filter.command("codex画图")
    async def image_command(self, event: AstrMessageEvent):
        """生成一张图；消息附图或引用图片时按图生图处理。"""
        async for result in self._image_command(event):
            yield result

    @filter.command("codex改图", alias={"codex图生图"})
    async def edit_command(self, event: AstrMessageEvent):
        """修改一张参考图；指令与图片同发，或引用带图消息。"""
        async for result in self._image_command(event, require_reference=True):
            yield result

    @filter.command("codex重绘")
    async def redraw_command(self, event: AstrMessageEvent):
        """按自己此前的绘图要求再生成一张。"""
        async for result in self._image_command(event, redraw=True):
            yield result

    async def _image_command(self, event, require_reference=False, redraw=False):
        paths = []
        reserved_key = None
        try:
            caller = self._check(event)
            use_api = await self._backend(caller) == "api"
            self._check(event)
            if use_api:
                self._require_api()
            key = API_JOB_KEY if use_api else self._account_key(caller)
            # Parse the original message to preserve whitespace/newlines in prompts.
            # AstrBot strips the wake prefix before dispatching commands.
            parts = event.get_message_str().strip().split(maxsplit=1)
            arguments = parts[1] if len(parts) == 2 else ""
            scope = (event.unified_msg_origin, caller, key)
            reference = reference_data = None
            if redraw:
                drawing_id = arguments or quoted_drawing_id(event)
                if not drawing_id or not DRAWING_ID.fullmatch(drawing_id):
                    raise PluginError("请引用带重绘编号的出图结果发送 /codex重绘，或直接发送 /codex重绘 编号。")
                drawing = self.redraw_history.get(drawing_id, scope)
                prompt, ratio, model = drawing.prompt, drawing.ratio, drawing.model
                reference_data, quality = drawing.reference, drawing.quality
            else:
                prompt, ratio = parse_drawing_prompt(arguments)
                if not prompt:
                    raise PluginError("用法：/codex改图 修改要求，并附带或引用一张图片" if require_reference else "用法：/codex画图 雨中的猫咪，水彩风格")
                reference = reference_from_message(event)
                if require_reference and reference is None:
                    raise PluginError("没有找到参考图，请将图片与 /codex改图 修改要求 一起发送，或引用一条带图消息。")
                drawing_id = uuid4().hex[:12]
                settings = self.service.settings
                model = settings.openai_image_model if use_api else settings.model
                quality = settings.openai_image_quality if use_api else None
            if key in self.image_owners or (key == SHARED_KEY and self.dashboard.login_pending):
                raise PluginError("绘图账号正在处理其他任务，请稍后重试。")
            if caller in self.image_owners.values():
                raise PluginError("你已有绘图任务，可发送 /codex取消 停止。")
            self.image_owners[key] = caller
            reserved_key = key
            prefix = "OpenAI API（按量计费）" if use_api else ""
            await event.send(event.plain_result(f"{prefix}正在生成图片，请稍候。可发送 /codex取消 停止任务。"))
            if key in self.cancelled_images:
                raise PluginError("任务已取消。")
            options = {}
            if reference is not None or reference_data is not None:
                async def loader():
                    nonlocal reference_data
                    if reference_data is None:
                        reference_data = await load_reference(
                            reference, self.service.settings.max_image_bytes, self.service.settings.request_timeout,
                        )
                    return reference_data
                options["reference_loader"] = loader
            if use_api:
                paths = await self.service.generate_api(
                    caller, prompt, ratio=ratio, model=model, quality=quality, **options,
                )
            else:
                if ratio is not None:
                    options["ratio"] = ratio
                if redraw or model:
                    options["model"] = model
                paths = await self.service.generate(key, prompt, **options)
            if key in self.cancelled_images:
                raise PluginError("任务已取消。")
            for path in paths:
                # Await delivery before deleting the temporary output.
                result = event.image_result(str(path))
                label = "OpenAI API · " if use_api else ""
                result.chain.append(Plain(f"{label}再次生成 /codex重绘 {drawing_id}"))
                await event.send(result)
            if paths and not self.service.closing:
                self.redraw_history.remember(
                    drawing_id, scope, DrawingRequest(prompt, ratio, model, reference_data, quality),
                )
        except PluginError as exc:
            yield event.plain_result(str(exc))
        except Exception as exc:
            logger.error("绘图异常 (%s)", type(exc).__name__)
            yield event.plain_result("绘图或图片发送失败，请检查当前绘图模式和消息平台。")
        finally:
            if reserved_key is not None:
                self.image_owners.pop(reserved_key, None)
                self.cancelled_images.discard(reserved_key)
            for path in paths:
                try:
                    path.unlink(missing_ok=True)
                    path.parent.rmdir()
                except OSError:
                    pass

    async def terminate(self):
        await self.dashboard.close()
        await self.service.close()
        self.redraw_history.clear()
