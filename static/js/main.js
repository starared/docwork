// 入口：身份检查、令牌兑换、路由、外框。
import { $, api, ApiError, clear, errToast, h, isOwner, post, state, toast } from './lib.js';
import * as home from './pages/home.js';
import * as create from './pages/create.js';
import * as worksPage from './pages/works.js';
import * as editor from './pages/editor.js';
import * as jobsPage from './pages/jobs.js';
import * as tools from './pages/tools.js';
import * as files from './pages/files.js';
import * as admin from './pages/admin.js';
import * as account from './pages/account.js';

const routes = [
  [/^\/$/, home.render],
  [/^\/create\/(ppt|doc|xls)$/, create.render],
  [/^\/works$/, worksPage.render],
  [/^\/work\/([\w-]+)$/, editor.render, true],
  [/^\/jobs$/, jobsPage.render],
  [/^\/tools\/(convert|pdf|ocr|import)$/, tools.render],
  [/^\/files$/, files.render],
  [/^\/admin\/(tokens|workspaces|models|settings|usage|system|audit)$/, admin.render],
  [/^\/account$/, account.render],
];

let cleanup = null;

async function boot() {
  // 令牌链接：#redeem=xxx（放在 # 片段中，不会写进服务器日志）
  const m = location.hash.match(/redeem=([^&]+)/);
  if (m) {
    const token = decodeURIComponent(m[1]);
    history.replaceState(null, '', location.pathname + '#/');
    try {
      await post('/api/redeem', { token });
      toast('已通过令牌进入', 'ok');
    } catch (e) {
      errToast(e);
    }
  }
  try {
    state.me = await api('/api/me', { allow401: true });
  } catch (e) {
    state.me = null;
  }
  if (!state.me || !state.me.authenticated) {
    state.me = null;
    renderLogin();
    return;
  }
  try { state.meta = await api('/api/meta'); } catch (e) { state.meta = {}; }
  window.addEventListener('hashchange', route);
  route();
}

function renderLogin() {
  const app = $('#app');
  const err = h('div', { class: 'error small' });
  const u = h('input', { type: 'text', autocomplete: 'username', placeholder: '用户名' });
  const p = h('input', { type: 'password', autocomplete: 'current-password', placeholder: '密码' });
  const t = h('input', { type: 'text', inputmode: 'numeric', placeholder: '两步验证码（如已开启）', autocomplete: 'one-time-code' });
  const tokenInput = h('input', { type: 'password', placeholder: '管理员发给你的访问令牌' });
  const doLogin = async (e) => {
    e.preventDefault();
    err.textContent = '';
    try {
      await post('/api/login', { username: u.value, password: p.value, totp: t.value });
      location.hash = '#/';
      location.reload();
    } catch (ex) { err.textContent = ex.message; }
  };
  const doRedeem = async (e) => {
    e.preventDefault();
    err.textContent = '';
    try {
      await post('/api/redeem', { token: tokenInput.value.trim() });
      location.hash = '#/';
      location.reload();
    } catch (ex) { err.textContent = ex.message; }
  };
  let mode = 'token';
  const body = h('div');
  const draw = () => {
    clear(body, mode === 'owner'
      ? h('form', { class: 'col', onsubmit: doLogin }, u, p, t, h('button', { class: 'btn primary', type: 'submit' }, '登录'),
        h('a', { href: '#', onclick: (e) => { e.preventDefault(); mode = 'token'; draw(); } }, '使用访问令牌进入'))
      : h('form', { class: 'col', onsubmit: doRedeem }, tokenInput, h('button', { class: 'btn primary', type: 'submit' }, '进入'),
        h('a', { href: '#', onclick: (e) => { e.preventDefault(); mode = 'owner'; draw(); } }, '管理员登录')));
  };
  draw();
  clear(app, h('div', { class: 'center-wrap' }, h('div', { class: 'card login' },
    h('h1', {}, 'DocWork 文档工作台'), body, err)));
}

function shell() {
  const app = $('#app');
  if ($('.side', app)) return $('#main');
  const me = state.me;
  const nav = h('nav', {},
    h('a', { href: '#/' }, '工作台'),
    h('div', { class: 'sec' }, '创建'),
    me.perms.includes('ppt') || isOwner() ? h('a', { href: '#/create/ppt' }, 'AI 生成 PPT') : null,
    me.perms.includes('doc') || isOwner() ? h('a', { href: '#/create/doc' }, 'AI 生成 Word') : null,
    me.perms.includes('xls') || isOwner() ? h('a', { href: '#/create/xls' }, 'AI 生成 Excel') : null,
    h('div', { class: 'sec' }, '作品与文件'),
    h('a', { href: '#/works' }, '作品库'),
    h('a', { href: '#/jobs' }, '任务中心'),
    h('a', { href: '#/files' }, '我的文件'),
    h('div', { class: 'sec' }, '工具'),
    me.perms.includes('import') || isOwner() ? h('a', { href: '#/tools/import' }, '导入已有文件') : null,
    me.perms.includes('convert') || isOwner() ? h('a', { href: '#/tools/convert' }, '格式转换') : null,
    me.perms.includes('pdf') || isOwner() ? h('a', { href: '#/tools/pdf' }, 'PDF 工具') : null,
    me.perms.includes('pdf') || isOwner() ? h('a', { href: '#/tools/ocr' }, '扫描件 OCR') : null,
    isOwner() ? [
      h('div', { class: 'sec' }, '后台'),
      h('a', { href: '#/admin/tokens' }, '临时令牌'),
      h('a', { href: '#/admin/workspaces' }, '工作区'),
      h('a', { href: '#/admin/models' }, '模型接口'),
      h('a', { href: '#/admin/usage' }, '用量统计'),
      h('a', { href: '#/admin/system' }, '系统状态'),
      h('a', { href: '#/admin/settings' }, '设置'),
      h('a', { href: '#/admin/audit' }, '审计日志'),
    ] : null,
  );
  const foot = h('div', { class: 'foot' },
    h('div', {}, isOwner() ? `管理员 ${me.username}` : `访客：${me.note || ''}`),
    !isOwner() && me.expires_at ? h('div', { class: 'muted small' }, `有效期至 ${new Date(me.expires_at * 1000).toLocaleString('zh-CN')}`) : null,
    h('div', { class: 'row gap', style: { marginTop: '6px' } },
      h('a', { href: '#/account' }, '账户'),
      h('a', { href: '#', onclick: async (e) => { e.preventDefault(); await post('/api/logout'); location.hash = '#/'; location.reload(); } }, '退出')));
  const main = h('div', { class: 'main', id: 'main' });
  // 手机上侧栏是顶栏，菜单按钮打开左侧抽屉；点遮罩、按 Esc 或跳转页面后关闭
  const side = h('aside', { class: 'side' });
  const setOpen = (open) => { side.classList.toggle('open', open); menuBtn.setAttribute('aria-expanded', String(open)); };
  const menuBtn = h('button', { class: 'icon-btn menu-btn', 'aria-label': '菜单', 'aria-expanded': 'false', onclick: () => setOpen(!side.classList.contains('open')) }, '☰');
  side.append(h('div', { class: 'logo' }, 'DocWork', h('small', {}, 'AI 文档工作台')), menuBtn,
    h('div', { class: 'side-back', onclick: () => setOpen(false) }), h('div', { class: 'drawer' }, nav, foot));
  nav.addEventListener('click', (e) => { if (e.target.closest('a')) setOpen(false); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') setOpen(false); });
  clear(app, side, main);
  if (me.disk && me.disk !== 'ok') toast(me.disk === 'block' ? '服务器磁盘空间不足，已暂停上传和新任务' : '服务器磁盘使用率较高', 'error', 8000);
  return main;
}

async function route() {
  const path = (location.hash.replace(/^#/, '') || '/').split('?')[0];
  if (cleanup) { try { cleanup(); } catch (e) { /* 忽略 */ } cleanup = null; }
  const main = shell();
  const side = $('.side');
  if (side) side.classList.remove('open');
  for (const a of document.querySelectorAll('.side nav a')) {
    a.classList.toggle('active', a.getAttribute('href') === '#' + path || (path.startsWith('/work/') && a.getAttribute('href') === '#/works'));
  }
  for (const [re, fn, full] of routes) {
    const m = path.match(re);
    if (m) {
      const page = h('div', { class: 'page' + (full ? ' full' : '') });
      clear(main, page);
      main.scrollTop = 0;
      try {
        cleanup = (await fn(page, ...m.slice(1))) || null;
      } catch (e) {
        errToast(e);
        clear(page, h('div', { class: 'empty' }, e.message));
      }
      return;
    }
  }
  clear(main, h('div', { class: 'page' }, h('div', { class: 'empty' }, '页面不存在')));
}

// 按钮里的 async 处理函数出错时统一提示，而不是静默失败（会话失效由 api() 处理，这里不重复提示）
window.addEventListener('unhandledrejection', (e) => {
  const err = e.reason;
  if (err instanceof ApiError && err.status === 401) { e.preventDefault(); return; }
  if (err instanceof ApiError || (err && typeof err.message === 'string' && !(err instanceof TypeError))) {
    errToast(err);
    e.preventDefault();
  }
});

boot();
