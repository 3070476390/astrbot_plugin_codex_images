from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .codex_images.errors import PluginError
from .codex_images.service import ImageService, Settings, user_key
from .codex_images.dashboard import DashboardController, SHARED_KEY
from .codex_images.message_images import load_reference, reference_from_message


class CodexImagesPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.image_owners = {}
        self.cancelled_images = set()
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
        stopped = await self.service.cancel(key)
        return reserved or stopped

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
            "Codex 订阅绘图\n" + account_help +
            "/codex状态：查询登录状态\n"
            "/codex画图 图片描述：生成一张图片\n"
            "/codex改图 修改要求 + 一张图：图生图，也可引用带图消息\n"
            "/codex模型 [页码]：查询账号可见的模型及图片输入能力\n"
            "/codex取消：停止当前登录或绘图任务\n"
            "是否能生成图片取决于账号权限和可用额度。"
        )

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
            state = await self.service.status(self._account_key(self._check(event, private=True)))
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
            key = self._account_key(caller)
            if self.account_mode == "shared" and self.image_owners.get(key) != caller:
                raise PluginError("你当前没有正在运行的绘图任务。")
            cancelled = await self.cancel_image(key)
            yield event.plain_result("已停止当前任务。" if cancelled else "当前没有正在运行的任务。")
        except PluginError as exc:
            yield event.plain_result(str(exc))

    @filter.command("codex模型", alias={"codex模型列表"})
    async def models_command(self, event: AstrMessageEvent):
        """查询当前绘图账号可见的调度模型，可带页码。"""
        try:
            key = self._account_key(self._check(event))
            parts = event.get_message_str().strip().split()
            if len(parts) > 2 or (len(parts) == 2 and (not parts[1].isascii() or not parts[1].isdigit() or len(parts[1]) > 6)):
                raise PluginError("用法：/codex模型 或 /codex模型 2")
            page = int(parts[1]) if len(parts) == 2 else 1
            if page < 1:
                raise PluginError("页码从 1 开始。")
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

    async def _image_command(self, event, require_reference=False):
        paths = []
        reserved_key = None
        try:
            caller = self._check(event)
            key = self._account_key(caller)
            # Parse the original message to preserve whitespace/newlines in prompts.
            # AstrBot strips the wake prefix before dispatching commands.
            parts = event.get_message_str().strip().split(maxsplit=1)
            prompt = parts[1] if len(parts) == 2 else ""
            if not prompt:
                raise PluginError("用法：/codex改图 修改要求，并附带或引用一张图片" if require_reference else "用法：/codex画图 雨中的猫咪，水彩风格")
            reference = reference_from_message(event)
            if require_reference and reference is None:
                raise PluginError("没有找到参考图，请将图片与 /codex改图 修改要求 一起发送，或引用一条带图消息。")
            if key in self.image_owners or (key == SHARED_KEY and self.dashboard.login_pending):
                raise PluginError("绘图账号正在处理其他任务，请稍后重试。")
            self.image_owners[key] = caller
            reserved_key = key
            await event.send(event.plain_result("正在生成图片，请稍候。可发送 /codex取消 停止任务。"))
            if key in self.cancelled_images:
                raise PluginError("任务已取消。")
            if reference is None:
                paths = await self.service.generate(key, prompt)
            else:
                async def loader():
                    return await load_reference(reference, self.service.settings.max_image_bytes, self.service.settings.request_timeout)
                paths = await self.service.generate(key, prompt, reference_loader=loader)
            for path in paths:
                # Await delivery before deleting the temporary output.
                await event.send(event.image_result(str(path)))
        except PluginError as exc:
            yield event.plain_result(str(exc))
        except Exception as exc:
            logger.error("Codex 绘图异常 (%s)", type(exc).__name__)
            yield event.plain_result("绘图或图片发送失败，请检查消息平台和 Codex 配置。")
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
