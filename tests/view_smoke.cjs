// Browser integration using a mock AstrBot bridge; never authenticates or calls Codex.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const schema = JSON.parse(fs.readFileSync(path.join(root, '_conf_schema.json'), 'utf8'));
const settings = Object.fromEntries(Object.entries(schema).map(([key, value]) => [key, value.default]));

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1200 }, deviceScaleFactor: 1 });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('http://127.0.0.1:9911/**', async route => {
      const name = new URL(route.request().url()).pathname.split('/').pop() || 'index.html';
      if (!['index.html', 'app.js', 'style.css'].includes(name)) return route.abort();
      await route.fulfill({ path: path.join(root, 'views/dashboard', name), contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html' });
    });
    await page.addInitScript(settings => {
      let state = {version: '0.5.0', settings, account: {status: 'signed_out', message: '尚未登录统一账号。'}, quota: null, login_pending: false, busy: false, active_tasks: 0};
      window.quotaMode = 'ready';
      window.quotaCalls = 0;
      window.setBusy = value => {state.busy = value;};
      window.rejectSave = false;
      window.finishLogin = () => {state.account = {status: 'logged_in', message: '统一账号登录成功，可以向机器人发送绘图指令。'};state.login_pending = false;};
      window.AstrBotPluginView = {
        ready: async () => ({}),
        apiGet: async () => structuredClone(state),
        apiPost: async (endpoint, body) => {
          if (endpoint.endsWith('/settings')) {
            if (window.rejectSave) throw new Error('模拟保存失败，请重试');
            state.settings = body;
            state.quota = null;
          }
          if (endpoint.endsWith('/login')) {
            state.account = {status: 'waiting', message: '请在浏览器新标签页打开官方地址，输入设备码完成登录。', code: 'TEST-1234', url: 'https://auth.openai.com/codex/device'};
            state.login_pending = true;
          }
          if (endpoint.endsWith('/cancel') || endpoint.endsWith('/logout')) {
            state.account = {status: 'signed_out', message: '统一账号已退出。'};
            state.login_pending = false;
            state.quota = null;
          }
          if (endpoint.endsWith('/quota')) {
            window.quotaCalls++;
            state.quota = window.quotaMode === 'error' ? {status: 'error', message: '额度查询失败，请检查网络。'} : {
              status: 'ready', data: {fetched_at: 1793700000, ordinary_usage_allowed: false, reset_credits: 0, truncated: false,
                buckets: window.quotaMode === 'empty' ? [] : [{name: 'codex', plan: 'plus', limit_message: null, spend_control_reached: false,
                  windows: [{label: '5 小时窗口', used_percent: 25, remaining_percent: 75, resets_at: 1793703600},
                            {label: '7 天窗口', used_percent: 100, remaining_percent: 0, resets_at: null}],
                  credits: {balance: '0', has_credits: false, unlimited: false}}]}
            };
          }
          return structuredClone(state);
        }
      };
    }, settings);
    await page.goto('http://127.0.0.1:9911/');
    await page.getByText('已连接插件后端').waitFor();
    assert.equal(await page.locator('#users-summary').innerText(), '仅聊天管理员');
    assert.equal(await page.locator('#refresh-quota').isDisabled(), true);
    await page.locator('#allowed_users').fill('qq_bot:123456789');
    await page.locator('#allow_group_images').check();
    await page.locator('#save').click();
    await page.getByText('设置已保存，将用于下一次任务。').waitFor();
    assert.equal(await page.locator('#users-summary').innerText(), '1 位用户 + 管理员');
    // Enabling all users keeps the existing allowlist for later restoration.
    assert.equal(await page.locator('#allow_all_users').isChecked(), false);
    await page.locator('#allow_all_users').check();
    assert.equal(await page.locator('#allowed_users').isDisabled(), true);
    assert.equal(await page.locator('#users-summary').innerText(), '1 位用户 + 管理员', 'unsaved draft must not change active summary');
    await page.locator('#reset').click();
    assert.equal(await page.locator('#allow_all_users').isChecked(), false);
    assert.equal(await page.locator('#allowed_users').isDisabled(), false);
    await page.locator('#allow_all_users').check();
    await page.locator('#save').click();
    assert.equal(await page.locator('#users-summary').innerText(), '所有用户');
    assert.equal(await page.locator('#allowed_users').inputValue(), 'qq_bot:123456789');
    await page.locator('#allow_all_users').uncheck();
    await page.locator('#save').click();
    assert.equal(await page.locator('#users-summary').innerText(), '1 位用户 + 管理员');
    assert.equal(await page.locator('#allowed_users').isDisabled(), false);
    assert.equal(await page.locator('#allowed_users').inputValue(), 'qq_bot:123456789');
    await page.locator('#allow_all_users').check();
    await page.locator('#save').click();
    await page.locator('#login').click();
    assert.equal(await page.locator('#device-code').inputValue(), 'TEST-1234');
    assert.equal(await page.locator('#device-panel').isVisible(), true);
    fs.mkdirSync(path.join(root, 'dist'), { recursive: true });
    await page.screenshot({ path: path.join(root, 'dist/dashboard-login-preview.png'), fullPage: true });
    // Polling must not overwrite unsaved drafts while login status changes.
    await page.locator('#model').fill('draft-model');
    await page.evaluate(() => window.finishLogin());
    await page.getByText('已连接 Codex', { exact: true }).waitFor();
    assert.equal(await page.locator('#model').inputValue(), 'draft-model');
    assert.equal(await page.locator('#device-panel').isVisible(), false);
    assert.equal(await page.locator('#device-code').inputValue(), '');
    assert.equal(await page.locator('#allow_all_users').isChecked(), true);
    assert.equal(await page.locator('#users-summary').innerText(), '所有用户');
    await page.evaluate(() => {window.rejectSave = true;});
    await page.locator('#save').click();
    await page.getByText('模拟保存失败，请重试').waitFor();
    assert.equal(await page.locator('#model').inputValue(), 'draft-model');
    await page.locator('#reset').click();
    assert.equal(await page.locator('#model').inputValue(), '');
    await page.locator('#refresh-quota').click();
    await page.getByText('剩余 75%', {exact: true}).waitFor();
    assert.equal(await page.locator('progress').first().getAttribute('value'), '75');
    assert.equal(await page.locator('progress').nth(1).getAttribute('value'), '0');
    assert.ok((await page.locator('#quota-content').innerText()).includes('重置：未提供'));
    assert.ok((await page.locator('#quota-content').innerText()).includes('Credits 余额：0'));
    assert.ok((await page.locator('#quota-status').innerText()).includes('本地时间'));
    await page.evaluate(() => window.setBusy(true));
    await page.waitForFunction(() => document.querySelector('#refresh-quota').disabled);
    assert.equal(await page.evaluate(() => window.quotaCalls), 1, 'state polling must not fetch quota');
    await page.evaluate(() => window.setBusy(false));
    await page.waitForFunction(() => !document.querySelector('#refresh-quota').disabled);
    fs.mkdirSync(path.join(root, 'dist'), { recursive: true });
    await page.screenshot({ path: path.join(root, 'dist/dashboard-preview.png'), fullPage: true });
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'));
    await page.waitForTimeout(200);
    await page.screenshot({ path: path.join(root, 'dist/dashboard-dark.png'), fullPage: true });
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'light'));
    await page.waitForTimeout(200);
    await page.setViewportSize({ width: 390, height: 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'mobile overflow');
    await page.screenshot({ path: path.join(root, 'dist/dashboard-mobile.png'), fullPage: true });
    await page.evaluate(() => {window.quotaMode = 'empty';});
    await page.locator('#refresh-quota').click();
    await page.getByText('服务暂未提供额度数据，不代表额度为零。').waitFor();
    assert.equal(await page.locator('progress').count(), 0);
    await page.evaluate(() => {window.quotaMode = 'error';});
    await page.locator('#refresh-quota').click();
    await page.getByText('额度查询失败，请检查网络。', {exact: true}).waitFor();
    assert.equal(await page.locator('progress').count(), 0);
    await page.evaluate(() => {window.rejectSave = false;});
    await page.locator('#account_mode').selectOption('personal');
    await page.locator('#save').click();
    await page.getByText('独立账号模式：请私聊发送 /codex额度 查询自己的账号。').waitFor();
    assert.equal(await page.locator('#users-summary').innerText(), '所有独立账号用户');
    assert.equal(await page.locator('#refresh-quota').isDisabled(), true);
    await page.locator('#logout').click();
    await page.getByText('统一账号已退出。').first().waitFor();
    assert.equal(await page.locator('progress').count(), 0);
    assert.deepEqual(errors, []);
    console.log('PASS: all-users switch/save/reset, allowlist restoration, account/settings flow, quota states, busy/personal restrictions, logout cleanup, mobile layout and themes');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error);process.exitCode = 1;});
