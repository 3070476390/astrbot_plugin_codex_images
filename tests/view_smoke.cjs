// Browser integration using a mock AstrBot bridge; never authenticates or calls Codex.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const schema = JSON.parse(fs.readFileSync(path.join(root, '_conf_schema.json'), 'utf8'));
const settings = Object.fromEntries(Object.entries(schema).filter(([, value]) => !value.secret).map(([key, value]) => [key, value.default]));

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1200 }, deviceScaleFactor: 1 });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('http://127.0.0.1:9911/**', async route => {
      const name = new URL(route.request().url()).pathname.split('/').pop() || 'index.html';
      if (!['index.html', 'app.js', 'gallery.js', 'style.css'].includes(name)) return route.abort();
      await route.fulfill({ path: path.join(root, 'views/dashboard', name), contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html' });
    });
    await page.addInitScript(settings => {
      let state = {version: '0.8.3', settings, account: {status: 'signed_out', message: '尚未登录统一账号。'}, openai_key_configured: false, openai_key_revision: 'initial', quota: null, login_pending: false, busy: false, active_tasks: 0};
      const thumbnail = 'UklGRjYAAABXRUJQVlA4ICoAAACQAQCdASoIAAYAAUAmJaACdLoAA5gA/tom/xbSOhU6v/jeOtvrb2UAAAA=';
      const gallery = Array.from({length: 13}, (_, index) => ({
        id: index.toString(16).padStart(32, '0'), created: 1793700000 - index * 5,
        backend: index === 0 ? 'api' : 'codex', model: index === 0 ? 'gpt-image-2' : 'Codex',
        prompt: index === 0 ? '窗边的橘猫' : `湖边风景 ${index}`,
        width: 8, height: 6, thumbnail,
      }));
      window.quotaMode = 'ready';
      window.quotaCalls = 0;
      window.keyChanges = [];
      window.openAIModelCalls = 0;
      window.openAIModels = ['gpt-image-2', 'gpt-image-1.5'];
      window.rejectModelList = false;
      window.rotateKey = () => { state.openai_key_revision = 'changed-externally'; };
      window.setBusy = value => {state.busy = value;};
      window.rejectSave = false;
      window.finishLogin = () => {state.account = {status: 'logged_in', message: '统一账号登录成功，可以向机器人发送绘图指令。'};state.login_pending = false;};
      window.AstrBotPluginView = {
        ready: async () => ({}),
        apiGet: async (endpoint, params) => {
          if (endpoint === 'dashboard/gallery') {
            const items = gallery.filter(item => (!params.backend || item.backend === params.backend) && item.prompt.includes(params.q));
            const page = Math.min(params.page, Math.max(1, Math.ceil(items.length / 12)));
            return {items: items.slice((page - 1) * 12, page * 12), page, pages: Math.max(1, Math.ceil(items.length / 12)), total: items.length, enabled: state.settings.gallery_enabled};
          }
          if (endpoint === 'dashboard/gallery-preview') return {image: thumbnail};
          return structuredClone(state);
        },
        download: async (endpoint, params) => {window.downloaded = {endpoint, id: params.id};},
        apiPost: async (endpoint, body) => {
          if (endpoint === 'dashboard/openai-models') {
            window.openAIModelCalls++;
            if (!state.openai_key_configured || Object.keys(body).length) throw new Error('模型查询应只使用已保存的 API Key。');
            if (window.rejectModelList) throw new Error('模拟模型目录读取失败。');
            return { models: structuredClone(window.openAIModels) };
          }
          if (endpoint.endsWith('/settings')) {
            if (window.rejectSave) throw new Error('模拟保存失败，请重试');
            const {openai_api_key: apiKey, ...settings} = body;
            if (apiKey !== undefined) {
              window.keyChanges.push(apiKey);
              state.openai_key_configured = Boolean(apiKey);
              state.openai_key_revision = `saved-${window.keyChanges.length}`;
            }
            state.settings = settings;
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
    assert.equal(await page.locator('#openai_api_enabled').isChecked(), false);
    assert.equal(await page.locator('#gallery_enabled').isChecked(), true);
    assert.equal(await page.locator('#openai-key-status').innerText(), '尚未配置 API Key。填写后点击保存设置。');
    assert.equal(await page.locator('#refresh-openai-models').isDisabled(), true);
    assert.equal(await page.evaluate(() => window.openAIModelCalls), 0);
    await page.locator('#openai_api_enabled').check();
    await page.locator('#openai_api_key').fill('mock-api-key');
    await page.locator('#openai_image_quality').selectOption('low');
    await page.locator('#allowed_users').fill('qq_bot:123456789');
    await page.locator('#allow_group_images').check();
    await page.locator('#save').click();
    await page.getByText('设置已保存，将用于下一次任务。').waitFor();
    assert.equal(await page.locator('#users-summary').innerText(), '1 位用户 + 管理员');
    assert.equal(await page.locator('#openai_api_enabled').isChecked(), true);
    await page.getByText('已读取 2 个图片模型。', {exact: false}).waitFor();
    assert.equal(await page.locator('#openai_image_model').inputValue(), settings.openai_image_model);
    assert.equal(await page.locator('#openai_image_model option[value="gpt-image-2"]').count(), 1);
    await page.locator('#openai_image_model').selectOption('gpt-image-2');
    await page.evaluate(() => { window.rejectModelList = true; });
    await page.locator('#refresh-openai-models').click();
    await page.getByText('模拟模型目录读取失败。', {exact: true}).waitFor();
    assert.equal(await page.locator('#openai_image_model').inputValue(), 'gpt-image-2');
    await page.evaluate(() => {
      window.rejectModelList = false;
      window.openAIModels = ['gpt-image-1.5'];
      window.rotateKey();
    });
    await page.getByText('已读取 1 个图片模型。', {exact: false}).waitFor();
    assert.equal(await page.locator('#openai_image_model').inputValue(), 'gpt-image-2');
    assert.equal(await page.locator('#openai_image_model option[value="gpt-image-1.5"]').count(), 1);
    assert.equal(await page.locator('#openai_image_model option[value="gpt-image-2.6-flare"]').count(), 0);
    await page.locator('#save').click();
    await page.getByText('设置已保存，将用于下一次任务。').waitFor();
    assert.equal(await page.locator('#openai_image_model').inputValue(), 'gpt-image-2');
    assert.equal(await page.evaluate(() => window.openAIModelCalls), 3);
    assert.equal(await page.locator('#openai_image_quality').inputValue(), 'low');
    assert.equal(await page.locator('#openai_api_key').inputValue(), '');
    assert.ok((await page.locator('#openai-key-status').innerText()).includes('已保存 API Key'));
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
    await page.setViewportSize({ width: 1440, height: 1200 });
    await page.getByRole('button', {name: '图库', exact: true}).click();
    await page.getByText('共 13 张', {exact: true}).waitFor();
    assert.equal(await page.locator('.gallery-item').count(), 12);
    await page.waitForFunction(() => document.querySelector('.gallery-item img').naturalWidth === 8);
    await page.screenshot({ path: path.join(root, 'dist/gallery-preview.png'), fullPage: true });
    await page.getByRole('button', {name: '下一页', exact: true}).click();
    await page.getByText('第 2 / 2 页', {exact: true}).waitFor();
    assert.equal(await page.locator('.gallery-item').count(), 1);
    await page.locator('#gallery-backend').selectOption('api');
    await page.getByText('共 1 张', {exact: true}).waitFor();
    await page.locator('#gallery-search').fill('不存在');
    await page.getByText('没有符合条件的图片。', {exact: true}).waitFor();
    await page.locator('#gallery-search').fill('橘猫');
    await page.getByRole('button', {name: /窗边的橘猫/}).click();
    await page.getByRole('dialog').waitFor();
    await page.waitForFunction(() => document.querySelector('#gallery-preview-image').naturalWidth === 8);
    await page.getByRole('button', {name: '下载原图', exact: true}).click();
    assert.deepEqual(await page.evaluate(() => window.downloaded), {endpoint: 'dashboard/gallery-download', id: '0'.repeat(32)});
    await page.getByRole('button', {name: '关闭', exact: true}).click();
    await page.setViewportSize({ width: 390, height: 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'mobile gallery overflow');
    await page.getByRole('button', {name: '绘图设置', exact: true}).click();
    await page.locator('#settings-form').waitFor();
    assert.deepEqual(await page.evaluate(() => window.keyChanges), ['mock-api-key']);
    await page.locator('#clear-openai-key').check();
    assert.equal(await page.locator('#openai_api_key').isDisabled(), true);
    await page.locator('#save').click();
    await page.getByText('尚未配置 API Key。填写后点击保存设置。', {exact: true}).waitFor();
    assert.deepEqual(await page.evaluate(() => window.keyChanges), ['mock-api-key', '']);
    assert.equal(await page.locator('#refresh-openai-models').isDisabled(), true);
    assert.equal(await page.locator('#openai_image_model option[value="gpt-image-1.5"]').count(), 0);
    assert.deepEqual(errors, []);
    console.log('PASS: settings, API key and image-model selection, account, quota, gallery and responsive layout');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error);process.exitCode = 1;});
