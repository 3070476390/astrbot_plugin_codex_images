class PluginError(Exception):
    """A safe, user-facing error; never include raw upstream responses."""


class RpcError(PluginError):
    def __init__(self, method: str, code: int | None = None):
        self.method = method
        self.code = code
        super().__init__(
            f"Codex 请求 {method} 失败（错误码 {code}）。"
            "请检查 Codex 版本、网络、登录状态及账号权限。"
        )
