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
  $("refresh-quota").disabled = blocked || busy || current?.settings.account_mode !== "shared" || current?.account.status !== "logged_in";
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
  renderQuota(state);
  controls();
}

function quotaText(parent, tag, text, className = "") {
  const node = document.createElement(tag);
  node.textContent = text;
  node.className = className;
  parent.append(node);
  return node;
}

function quotaTime(timestamp) {
  return timestamp == null ? "未提供" : new Date(timestamp * 1000).toLocaleString("zh-CN", {hour12: false});
}

function renderQuota(state) {
  const content = $("quota-content");
  content.replaceChildren();
  const status = $("quota-status");
  status.classList.remove("danger-text");
  if (state.settings.account_mode !== "shared") {
    status.textContent = "独立账号模式：请私聊发送 /codex额度 查询自己的账号。";
    return;
  }
  const quota = state.quota;
  if (!quota) {
    status.textContent = state.account.status === "logged_in" ? "点击刷新额度，读取统一账号的最新用量。" : "确认统一账号已登录后，可刷新额度。";
    return;
  }
  if (quota.status === "error") {
    status.textContent = quota.message;
    status.classList.add("danger-text");
    return;
  }
  const data = quota.data;
  status.textContent = `上次查询：${quotaTime(data.fetched_at)}（本地时间）`;
  if (data.ordinary_usage_allowed === false) quotaText(content, "p", "服务当前不允许使用订阅内额度。", "quota-warning");
  if (!data.buckets.length) quotaText(content, "p", "服务暂未提供额度数据，不代表额度为零。", "quota-empty");
  for (const bucket of data.buckets) {
    const group = document.createElement("div");
    group.className = "quota-bucket";
    content.append(group);
    quotaText(group, "h4", bucket.name + (bucket.plan ? ` · ${bucket.plan}` : ""));
    if (!bucket.windows.length) quotaText(group, "p", "用量窗口：未提供", "muted");
    for (const window of bucket.windows) {
      const row = document.createElement("div");
      row.className = "quota-window";
      group.append(row);
      quotaText(row, "p", window.label, "quota-window-label");
      if (window.remaining_percent == null) {
        quotaText(row, "p", "用量未提供", "muted");
      } else {
        quotaText(row, "strong", `剩余 ${window.remaining_percent}%`, "quota-remaining");
        const progress = document.createElement("progress");
        progress.max = 100;
        progress.value = window.remaining_percent;
        progress.setAttribute("aria-label", `${bucket.name} ${window.label} 剩余额度`);
        if (window.remaining_percent <= 20) progress.classList.add("low");
        row.append(progress);
        quotaText(row, "p", `已用 ${window.used_percent}%`, "muted");
      }
      quotaText(row, "p", `重置：${quotaTime(window.resets_at)}${window.resets_at == null ? "" : "（本地时间）"}`, "muted");
    }
    const credits = bucket.credits;
    if (credits) {
      const balance = credits.unlimited ? "不限额" : credits.balance ?? (credits.has_credits === true ? "有可用额度，数值未提供" : credits.has_credits === false ? "无可用额度" : "未提供");
      quotaText(group, "p", `Credits 余额：${balance}`, "quota-credits");
    }
    if (bucket.limit_message) quotaText(group, "p", bucket.limit_message, "quota-warning");
    if (bucket.spend_control_reached) quotaText(group, "p", "已达到支出控制限制。", "quota-warning");
  }
  if (data.reset_credits != null) quotaText(content, "p", `可用额度重置次数：${data.reset_credits}（仅展示）`, "quota-credits");
  if (data.truncated) quotaText(content, "p", "仅展示前 20 个额度分组。", "muted");
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
$("refresh-quota").addEventListener("click", () => { void action("quota"); });
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
