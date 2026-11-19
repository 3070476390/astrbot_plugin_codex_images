# AstrBot Codex 订阅绘图

通过官方 Codex CLI 的 app-server 协议，让每个用户使用自己的 ChatGPT/Codex 订阅账号登录，调用 Codex 内置图像生成，并由 AstrBot 回传图片。

**版本：0.1.0，首版，待真实账号及消息平台联调。** 已实现设备码登录、状态查询、退出、文生图、取消任务、超时和并发控制。无需填写 OpenAI API Key，也不会自动切换到按量付费 API。

## 运行条件

- AstrBot 4.9.2 或以上的 4.x 版本，Python 3.11+。
- 在 **AstrBot 所在的机器或容器内** 安装官方 Codex CLI，且该版本必须支持 `chatgptDeviceCode` 登录和内置图像生成。宿主机安装 CLI 不等于 Docker 容器内可用。
- 用户账号具备 Codex 及图像生成权限、有可用额度，并能完成设备码登录。必要时在 ChatGPT 安全设置或工作区权限中开启设备码登录。
- 运行环境可访问 OpenAI 登录和 Codex 服务。子进程保留标准代理和证书环境变量。

本地验证所用 CLI 为 `0.166.0-alpha.5.1`：已验证真实协议初始化、空白账号状态及进程退出，已核对其生成的 JSON Schema。**没有把这次协议验证视为真实出图验证，也不保证其他 CLI 版本兼容。** app-server 接口和图片返回格式可能变化。

## 安装

1. 将 `dist/astrbot_plugin_codex_images-v0.1.0.zip` 上传到 AstrBot 插件管理的本地安装入口；也可将插件源码放入 `data/plugins/astrbot_plugin_codex_images/`。`main.py` 和 `metadata.yaml` 应位于该目录顶层。
2. 确保 AstrBot 进程能启动 Codex CLI；插件没有额外 Python 包依赖。
3. 在插件配置中填写 `codex_command`，通常 Linux 为 `codex` 或其绝对路径。Windows 必须填写 **原生 `codex.exe` 的完整路径**；不执行 npm 的 `.cmd`、`.bat` 或 PowerShell 包装脚本。
4. 重载插件，私聊机器人发送 `/codex帮助`。

如果使用官方 npm 包，在 Linux 上可通过 `npm install -g @openai/codex` 安装 CLI。以官方 [CLI 文档](https://learn.chatgpt.com/docs/cli) 为准。Windows 可使用已有桌面应用附带的 CLI 或官方原生二进制；升级后绝对路径可能变化，需要同步更新配置。

## 使用

```text
/codex登录
/codex状态
/codex画图 一只撑着透明雨伞的橘猫，水彩插画，柔和的蓝绿色背景
/codex取消
/codex退出
```

发送 `/codex登录` 后，机器人私聊返回官方 `auth.openai.com` 登录页面和一次性设备码。在自己的浏览器登录并输入设备码；成功后机器人会回复通知。不需要向机器人发送密码、令牌或 `auth.json`。

账号凭据由 Codex 保存在 **机器人服务器** 上，使用者应信任服务器管理员。每个平台实例、每个用户有各自的账号目录，不共享管理员账号。不同机器人平台之间不会自动关联账号。

默认仅允许私聊。开启 `allow_group_images` 后，群成员可使用自己已登录的账号绘图，图片会回传到发起请求的群。登录、状态查询和退出仍仅限私聊。

每次生成一张图，目前只支持文生图。没有实现参考图编辑、多图批量生成或跨平台账号绑定。

## 配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `codex_command` | `codex` | 可执行文件名称或完整路径，不接受命令行参数 |
| `model` | 空 | 使用 Codex 默认调度模型；可填写账号可用的 Codex 模型，不能填图像模型名 |
| `allow_group_images` | `false` | 是否允许群聊绘图 |
| `allowed_users` | `[]` | 空表示所有人可使用自己的账号；填写 `平台实例ID:用户ID` 限制使用者，可通过 `/sid` 查询 |
| `max_sessions` | `4` | 1–32；登录等待、状态查询和绘图均计入，同时每用户最多一个任务 |
| `request_timeout` | `30` | 单次协议请求超时，5–120 秒 |
| `login_timeout` | `900` | 等待用户完成登录，30–1800 秒；设备码可能更早失效 |
| `generation_timeout` | `300` | 单次绘图等待上限，30–1800 秒 |
| `max_image_mb` | `20` | 图片大小上限，1–30 MB |
| `max_prompt_chars` | `4000` | 描述长度上限，1–16000 字符 |

配置错误会导致插件加载失败，并指出对应字段。修改配置后重载插件。

## 数据和运行方式

数据位于 AstrBot 的 `data/plugin_data/astrbot_plugin_codex_images/`：

```text
users/<平台和用户标识的 SHA-256>/
  codex/                 # 独立 CODEX_HOME；Codex 管理凭据和刷新
  jobs/<随机任务标识>/     # 绘图工作目录和临时回传图片
```

- 凭据采用 Codex 文件存储，不复制或读取服务器默认 `~/.codex` 的登录信息，不继承 OpenAI API Key。
- 登录链接和设备码仅通过私聊发送；状态不展示邮箱、令牌或账号详细信息。日志不写入上游响应和 stderr，避免泄露凭据。
- 每个任务启动独立 app-server，任务结束、超时、取消或插件停止时回收。取消可能无法撤回已发生的服务端用量。
- 模型会话使用只读沙箱，禁用 shell、浏览器、电脑控制、插件、MCP 应用及子代理；客户端拒绝服务端权限申请和自定义工具请求。
- 账号目录隔离是应用层隔离，**不是独立的系统用户或容器安全边界**。建议在专用容器/系统账号下部署，并用使用名单限制为可信用户；不要把服务器上的敏感工作目录挂载给它。
- 只从 `imageGeneration` 完成事件提取图片，支持 Base64 和本用户工作目录或 `codex/generated_images` 下的 `savedPath`。不从模型自然语言中提取文件路径，不下载任意图片 URL。
- 回传副本在发送结束后删除；Codex 自己生成的原图、缓存和诊断文件可能保留，需由管理员按实际磁盘占用维护。退出登录不承诺清空所有历史缓存，卸载插件也不主动递归删除数据目录。
- 多进程 AstrBot 实例不应共享同一个插件数据目录；本版并发控制在单进程内生效。

## 验证和排障

```bash
python -m unittest discover -s tests -v
python scripts/smoke_codex.py --codex /path/to/codex
python scripts/build_zip.py
```

单元测试使用本地模拟 app-server，通过真实子进程和 JSONL 管道验证登录、通知先于响应、重复事件、用户隔离、取消、超时、异常退出、图片路径检查和消息处理。`smoke_codex.py` 使用临时空白账号目录验证真实 CLI 协议，不登录、不调用模型、不生成图片。

真实部署验收：私聊登录 → 状态显示已登录 → 生成一张图 → 图片可见 → 退出后状态显示未登录；再使用第二个消息账号确认无法使用第一个账号的登录状态。群聊开启时，应另外验证私聊和群聊使用同一个发送者账号。

常见问题：

- **找不到 Codex / 无法启动**：检查路径、容器内安装情况、可执行权限。Windows 的异步子进程需要可用的 Proactor 事件循环；某些执行沙箱会阻止创建子进程管道。
- **登录接口失败**：检查 CLI 是否支持设备码流程、账号是否允许设备码登录，以及网络代理。不会改用群聊收集凭据。
- **登录成功但无图片**：不代表图像权限可用。确认账号与 CLI 支持内置生图，并检查订阅额度和模型设置。
- **仅返回图片引用**：本版不猜测私有文件下载接口；只支持带 Base64 或受限本地路径的结果。需要使用兼容 CLI 或适配其公开的新协议。
- **只收到内部错误**：日志只含异常类型；可先用隔离环境运行上述协议检查。向他人提供排障材料前，请移除设备码和凭据。

## 接口依据

- [Codex app-server：协议与设备码登录](https://learn.chatgpt.com/docs/app-server)
- [Codex 认证](https://learn.chatgpt.com/docs/auth)
- [Codex 内置图像生成](https://learn.chatgpt.com/docs/image-generation)：内置生图计入 Codex 用量，具体账号可用性和额度以官方服务为准。
- [Sign in with ChatGPT 预览限制](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)：该令牌调用 Responses API 的路线当前不支持托管图像生成，因此本插件使用 Codex 管理登录和内置工具，未将订阅令牌当作 Images API Key。
- [AstrBot 插件开发](https://docs.astrbot.app/dev/star/plugin-new.html)

本仓库目前未发布到远程仓库或插件市场，`metadata.yaml` 的 `repo` 暂留空。发布时填写实际仓库 URL。
