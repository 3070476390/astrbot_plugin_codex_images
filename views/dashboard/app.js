const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const numeric = ["max_sessions", "request_timeout", "login_timeout", "generation_timeout", "max_image_mb", "max_prompt_chars"];
let current = null, dirty = false, operationPending = false, connected = false, sequence = 0, pollTimer;

function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
  $("notice").hidden = !message;
}

function connection(ok) {
  connected = ok;
  $("connection-dot").className = `dot ${ok ? "online" : "error"}`;
  $("connection-text").textContent = ok ? "已连接插件后端" : "连接暂不可用";
}

function fillForm(settings) {
  for (const [key, value] of Object.entries(settings)) {
    const element = $(key);
    if (!element) continue;
    if (element.type === "checkbox") element.checked = value;
    else element.value = Array.isArray(value) ? value.join("\n") : value;
  }
  dirty = false;
  formHints();
}

function formHints() {
  $("allowlist-hint").textContent = $("account_mode").value === "shared"
    ? "格式：平台实例ID:用户ID，可通过机器人 /sid 查询。统一模式下，聊天管理员始终可用；名单为空时仅管理员可用。"
    : "格式：平台实例ID:用户ID，每行一项。独立模式下，名单为空表示所有用户均可使用自己的账号。";
}

function controls() {
  const blocked = !connected || operationPending || !current;
  const busy = current?.busy || current?.login_pending;
  $("settings-fields").disabled = blocked;
  $("save").disabled = blocked || !dirty || busy || current?.active_tasks > 0;
  $("reset").disabled = blocked || !dirty;
  $("refresh").disabled = blocked || busy;
  $("login").disabled = blocked || busy || current?.settings.account_mode !== "shared" || current?.account.status === "logged_in";
  $("logout").disabled = blocked || current?.account.status === "signed_out";
  $("cancel").disabled = blocked || !busy;
  $("save-state").textContent = dirty ? "有未保存的修改" : current ? "设置已同步" : "正在读取";
  $("save-state").classList.toggle("dirty", dirty);
}

function render(state, resetForm = false) {
  current = state;
  if (resetForm) fillForm(state.settings);
  const labels = {unknown: "尚未检查", signed_out: "尚未登录", logged_in: "已连接 Codex", starting: "正在获取设备码", waiting: "等待完成授权", error: "登录未完成"};
  $("account-status").textContent = labels[state.account.status] || "状态待确认";
  $("account-message").textContent = state.account.message;
  const codeVisible = state.account.status === "waiting" && state.account.code && state.account.url;
  $("device-panel").hidden = !codeVisible;
  $("device-code").value = codeVisible ? state.account.code : "";
  $("device-url").value = codeVisible ? state.account.url : "";
  const shared = state.settings.account_mode === "shared";
  $("personal-note").hidden = shared;
  $("mode-summary").textContent = shared ? "统一绘图账号" : "每人独立账号";
  const count = state.settings.allowed_users.length;
  $("users-summary").textContent = shared ? count ? `${count} 位用户 + 管理员` : "仅聊天管理员" : count ? `${count} 位指定用户` : "所有独立账号用户";
  $("tasks-summary").textContent = `${state.active_tasks} / ${state.settings.max_sessions}`;
  $("group-summary").textContent = state.settings.allow_group_images ? "已开启" : "未开启";
  controls();
}

async function action(endpoint, payload = {}, resetForm = false) {
  operationPending = true;
  const requestId = ++sequence;
  controls();
  notice("");
  try {
    const state = await bridge.apiPost(`dashboard/${endpoint}`, payload);
    if (requestId === sequence) {
      connection(true);
      render(state, resetForm);
      if (endpoint === "settings") notice("设置已保存，将用于下一次任务。");
      if (endpoint === "logout") notice("统一账号已退出。");
    }
  } catch (error) {
    notice(error.message || "操作失败，请稍后重试。", true);
  } finally {
    operationPending = false;
    controls();
  }
}

async function poll() {
  if (!operationPending && !document.hidden) {
    const requestId = ++sequence;
    try {
      const state = await bridge.apiGet("dashboard/state");
      if (requestId === sequence) {
        connection(true);
        render(state, !current);
      }
    } catch (error) {
      if (requestId === sequence) {
        connection(false);
        notice(error.message || "无法读取状态，请检查后台连接。", true);
        controls();
      }
    }
  }
  pollTimer = setTimeout(poll, 2500);
}

async function copyField(id) {
  const field = $(id);
  try {
    field.focus();
    field.select();
    // AstrBot's restricted iframe may not grant the Clipboard API. Selection
    // remains available if execCommand is also unsupported.
    if (!document.execCommand("copy")) throw new Error("manual");
    notice("已复制。");
  } catch {
    notice("内容已选中，请按 Ctrl+C（Mac 使用 ⌘C）复制。");
  }
}

$("settings-form").addEventListener("input", () => { dirty = true; formHints(); controls(); });
$("settings-form").addEventListener("change", () => { dirty = true; formHints(); controls(); });
$("settings-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!current || operationPending) return;
  const payload = {};
  for (const key of Object.keys(current.settings)) {
    const element = $(key);
    if (!element) continue;
    if (element.type === "checkbox") payload[key] = element.checked;
    else if (numeric.includes(key)) payload[key] = Number(element.value);
    else if (key === "allowed_users") payload[key] = element.value.split(/\r?\n/).map(v => v.trim()).filter(Boolean);
    else payload[key] = element.value.trim();
  }
  void action("settings", payload, true);
});
$("reset").addEventListener("click", () => { fillForm(current.settings); controls(); notice(""); });
$("login").addEventListener("click", () => { void action("login"); });
$("refresh").addEventListener("click", () => { void action("refresh"); });
$("logout").addEventListener("click", () => { void action("logout"); });
$("cancel").addEventListener("click", () => { void action("cancel"); });
$("copy-code").addEventListener("click", () => { void copyField("device-code"); });
$("copy-url").addEventListener("click", () => { void copyField("device-url"); });
window.addEventListener("pagehide", () => clearTimeout(pollTimer));

try {
  if (!bridge) throw new Error("请从 AstrBot 的“扩展功能 → Codex 绘图”打开此页面。");
  await bridge.ready();
  const state = await bridge.apiGet("dashboard/state");
  connection(true);
  render(state, true);
  if (!state.busy && !state.login_pending) await action("refresh");
  void poll();
} catch (error) {
  connection(false);
  notice(error.message || "页面初始化失败，请刷新重试。", true);
  controls();
}
