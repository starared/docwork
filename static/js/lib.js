// 公共工具：DOM 构建、接口调用、上传、任务进度、弹窗、提示、格式化。

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
      else if (k === 'html') el.innerHTML = v;
      else if (k === 'value') el.value = v;
      else if (k === 'checked') el.checked = !!v;
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else el.setAttribute(k, v === true ? '' : v);
    }
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export function clear(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

export const $ = (sel, root = document) => root.querySelector(sel);

// ---------- 接口 ----------

export const state = { me: null, meta: null };

export function csrfToken() { return csrf(); }

function csrf() {
  const m = document.cookie.match(/(?:^|; )dw_csrf=([^;]+)/);
  return (state.me && state.me.csrf) || (m ? decodeURIComponent(m[1]) : '');
}

export class ApiError extends Error {
  constructor(msg, status, code) { super(msg); this.status = status; this.code = code; }
}

export async function api(path, opts = {}) {
  const method = opts.method || (opts.body !== undefined ? 'POST' : 'GET');
  const headers = { ...(opts.headers || {}) };
  let body = opts.body;
  if (body !== undefined && !(body instanceof Blob) && !(body instanceof ArrayBuffer)) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(body);
  }
  if (method !== 'GET') headers['X-CSRF-Token'] = csrf();
  let r;
  try {
    r = await fetch(path, { method, headers, body, credentials: 'same-origin' });
  } catch (e) {
    throw new ApiError('网络连接失败，请检查网络后重试', 0, 'network');
  }
  if (r.status === 401 && !opts.allow401) {
    // 会话失效：重新加载让 boot() 显示登录框（令牌链接的兑换流程除外）
    state.me = null;
    if (!location.hash.includes('redeem=')) { location.hash = '#/'; location.reload(); }
    throw new ApiError('登录已失效，请重新登录', 401, 'unauthenticated');
  }
  const ct = r.headers.get('content-type') || '';
  const data = ct.includes('application/json') ? await r.json() : await r.text();
  if (!r.ok) throw new ApiError((data && data.error) || `请求失败（${r.status}）`, r.status, data && data.code);
  return data;
}

export const get = (p) => api(p);
export const post = (p, body = {}) => api(p, { method: 'POST', body });
export const patch = (p, body = {}) => api(p, { method: 'PATCH', body });
export const put = (p, body = {}) => api(p, { method: 'PUT', body });
export const del = (p) => api(p, { method: 'DELETE' });

export function fileUrl(fid, inline = false) {
  return `/api/files/${encodeURIComponent(fid)}/download${inline ? '?inline=1' : ''}`;
}

export function download(fid) {
  const a = h('a', { href: fileUrl(fid), download: '' });
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// ---------- 分片上传（可续传） ----------

export async function uploadFile(file, { onProgress, purpose = 'source' } = {}) {
  const key = `dw_up_${file.name}_${file.size}_${file.lastModified}`;
  let info = null;
  try { info = JSON.parse(localStorage.getItem(key) || 'null'); } catch (e) { info = null; }
  let received = new Set();
  if (info) {
    try {
      const st = await api(`/api/uploads/${info.upload_id}`);
      received = new Set(st.received);
    } catch (e) { info = null; }
  }
  if (!info) {
    info = await post('/api/uploads', { name: file.name, size: file.size });
    try { localStorage.setItem(key, JSON.stringify(info)); } catch (e) { /* 忽略 */ }
  }
  const cs = info.chunk_size;
  const n = Math.max(1, Math.ceil(file.size / cs));
  let done = received.size;
  for (let i = 0; i < n; i++) {
    if (received.has(i)) continue;
    const blob = file.slice(i * cs, Math.min(file.size, (i + 1) * cs));
    let tries = 0;
    for (;;) {
      try {
        await api(`/api/uploads/${info.upload_id}/${i}`, { method: 'PUT', body: blob, headers: { 'Content-Type': 'application/octet-stream' } });
        break;
      } catch (e) {
        if (++tries >= 4 || (e.status && e.status < 500 && e.status !== 0)) throw e;
        await sleep(1000 * tries);
      }
    }
    done++;
    onProgress && onProgress(done / n);
  }
  const rec = await post(`/api/uploads/${info.upload_id}/complete`, { purpose });
  try { localStorage.removeItem(key); } catch (e) { /* 忽略 */ }
  return rec;
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------- 任务 ----------

// 一个页面只开一条推送流：所有任务卡片共用 /api/jobs/events?ids=…，关注的任务变化时重开连接。
// 浏览器对同一域名只给 6 条 HTTP/1.1 连接，每张卡片各开一条流会把其它请求卡住。
// 推送不可用时退回批量轮询（一次请求查全部任务），30 秒后再尝试推送。
const feed = { subs: new Map(), es: null, timer: null, pollTimer: null, polling: false, polls: 0 };

function feedIds() { return [...feed.subs.keys()]; }

function feedDispatch(j) {
  const set = feed.subs.get(j.id);
  if (!set) return;
  const done = ['done', 'failed', 'cancelled', 'awaiting_input'].includes(j.status);
  if (done) feed.subs.delete(j.id);
  for (const cb of [...set]) { try { cb(j); } catch (e) { console.error(e); } }
  if (done) feedSchedule();
}

function feedSchedule() {
  clearTimeout(feed.timer);
  feed.timer = setTimeout(feedOpen, 30);
}

function feedClose() {
  if (feed.es) { feed.es.close(); feed.es = null; }
  clearTimeout(feed.pollTimer);
  feed.pollTimer = null;
}

function feedOpen() {
  feedClose();
  const ids = feedIds();
  if (!ids.length) return;
  if (feed.polling) { feedPoll(); return; }
  let es;
  try { es = new EventSource(`/api/jobs/events?ids=${ids.map(encodeURIComponent).join(',')}`); } catch (e) { feed.polling = true; feedPoll(); return; }
  feed.es = es;
  es.addEventListener('job', (ev) => feedDispatch(JSON.parse(ev.data)));
  es.addEventListener('denied', () => { feedClose(); for (const id of feedIds()) feedDispatch({ id, status: 'failed', error: '登录已失效' }); });
  es.onerror = () => {
    if (feed.es !== es) return;
    feedClose();
    if (!feedIds().length) return;
    feed.polling = true;
    feed.polls = 0;
    feedPoll();
  };
}

async function feedPoll() {
  const ids = feedIds();
  if (!ids.length) { feed.polling = false; return; }
  try {
    const r = await api(`/api/jobs?ids=${ids.map(encodeURIComponent).join(',')}`);
    const got = new Set();
    for (const j of r.items) { got.add(j.id); feedDispatch(j); }
    // 不存在或无权查看的任务：按失败结束，不再无限等待
    for (const id of ids) if (!got.has(id) && feed.subs.has(id)) feedDispatch({ id, status: 'failed', error: '任务不存在或无权查看' });
  } catch (e) {
    if ([401, 403].includes(e.status)) { for (const id of feedIds()) feedDispatch({ id, status: 'failed', error: e.message }); return; }
  }
  if (!feedIds().length) { feed.polling = false; return; }
  if (++feed.polls >= 20) { feed.polling = false; feedSchedule(); return; }  // 轮询约 30 秒后再试推送
  feed.pollTimer = setTimeout(feedPoll, 1500);
}

export function watchJob(jobId, onUpdate) {
  let set = feed.subs.get(jobId);
  if (!set) { set = new Set(); feed.subs.set(jobId, set); }
  set.add(onUpdate);
  feedSchedule();
  return () => {
    const cur = feed.subs.get(jobId);
    if (!cur) return;
    cur.delete(onUpdate);
    if (!cur.size) { feed.subs.delete(jobId); feedSchedule(); }
  };
}

export const JOB_STATUS = {
  queued: '排队中', running: '运行中', awaiting_input: '等待确认', cancelling: '正在取消', cancelled: '已取消', done: '完成', failed: '失败',
};

export function jobCard(job, { onDone, compact = false } = {}) {
  const bar = h('div', { class: 'bar' }, h('i'));
  const status = h('span', { class: 'badge' });
  const stage = h('div', { class: 'muted small' });
  const stream = h('pre', { class: 'stream', hidden: true });
  const actions = h('div', { class: 'row gap' });
  const err = h('div', { class: 'error small', hidden: true });
  const el = h('div', { class: 'jobcard' + (compact ? ' compact' : '') },
    h('div', { class: 'row between' }, h('strong', {}, job.title || job.kind), status), bar, stage, err, stream, actions);
  let stop = null;
  let doneCalled = false;
  const render = (j) => {
    status.textContent = JOB_STATUS[j.status] || j.status;
    status.className = 'badge ' + j.status;
    bar.firstChild.style.width = `${Math.round((j.progress || 0) * 100)}%`;
    const parts = [j.stage, j.message, j.child && j.child.stage && j.child.stage !== j.stage ? `（${j.child.stage}）` : ''].filter(Boolean);
    stage.textContent = parts.join(' · ');
    if (j.stream && j.status === 'running') {
      stream.hidden = false;
      stream.textContent = j.stream.slice(-1500);
      stream.scrollTop = stream.scrollHeight;
    } else stream.hidden = true;
    if (j.status === 'failed') { err.hidden = false; err.textContent = j.error || '任务失败'; }
    clear(actions);
    if (['queued', 'running', 'awaiting_input'].includes(j.status)) {
      actions.append(h('button', { class: 'btn small ghost', onclick: async () => { await post(`/api/jobs/${j.id}/cancel`); toast('已请求取消'); } }, '取消'));
    }
    if (['failed', 'cancelled'].includes(j.status)) {
      actions.append(h('button', { class: 'btn small ghost', onclick: async () => {
        const nj = await post(`/api/jobs/${j.id}/retry`);
        el.replaceWith(jobCard(nj, { onDone, compact }));
      } }, '重试'));
    }
    if (['done', 'failed', 'cancelled', 'awaiting_input'].includes(j.status) && !doneCalled) {
      doneCalled = true;
      onDone && onDone(j, el);
    }
  };
  render(job);
  if (!['done', 'failed', 'cancelled'].includes(job.status)) stop = watchJob(job.id, render);
  el._stop = () => stop && stop();
  return el;
}

// ---------- 提示与弹窗 ----------

export function toast(msg, kind = 'info', ms = 3200) {
  let box = $('#toasts');
  if (!box) { box = h('div', { id: 'toasts' }); document.body.appendChild(box); }
  const t = h('div', { class: `toast ${kind}` }, msg);
  box.appendChild(t);
  setTimeout(() => t.classList.add('show'), 10);
  setTimeout(() => { t.classList.remove('show'); setTimeout(() => t.remove(), 300); }, ms);
}

export function errToast(e) {
  console.error(e);
  toast(e && e.message ? e.message : String(e), 'error', 5000);
}

export function modal(title, body, { actions = [], wide = false, onClose } = {}) {
  const close = () => { back.remove(); document.removeEventListener('keydown', esc); onClose && onClose(); };
  const esc = (e) => { if (e.key === 'Escape') close(); };
  const foot = h('div', { class: 'modal-foot' });
  for (const a of actions) {
    foot.append(h('button', { class: `btn ${a.kind || ''}`, onclick: async (ev) => {
      const btn = ev.currentTarget;
      btn.disabled = true;
      try { const r = await a.onClick(close); if (r !== false && a.close !== false) close(); } catch (e) { errToast(e); } finally { btn.disabled = false; }
    } }, a.label));
  }
  const box = h('div', { class: 'modal' + (wide ? ' wide' : ''), role: 'dialog' },
    h('div', { class: 'modal-head' }, h('h3', {}, title), h('button', { class: 'icon-btn', title: '关闭', onclick: close }, '×')),
    h('div', { class: 'modal-body' }, body), actions.length ? foot : null);
  const back = h('div', { class: 'modal-back', onmousedown: (e) => { if (e.target === back) close(); } }, box);
  document.body.appendChild(back);
  document.addEventListener('keydown', esc);
  return { close, el: box };
}

export function confirmDialog(title, text, okLabel = '确定', danger = false) {
  return new Promise((resolve) => {
    let ok = false;
    modal(title, h('p', {}, text), {
      actions: [
        { label: '取消', kind: 'ghost', onClick: () => {} },
        { label: okLabel, kind: danger ? 'danger' : 'primary', onClick: () => { ok = true; } },
      ],
      onClose: () => resolve(ok),
    });
  });
}

// ---------- 格式化 ----------

export function fmtTime(t) {
  if (!t) return '';
  const d = new Date(t * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  const now = new Date();
  const same = d.toDateString() === now.toDateString();
  return same ? `${pad(d.getHours())}:${pad(d.getMinutes())}` : `${d.getMonth() + 1}月${d.getDate()}日 ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function fmtDateTime(t) {
  if (!t) return '';
  const d = new Date(t * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function fmtSize(n) {
  if (n === null || n === undefined) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

export function fmtNum(n) {
  return (n || 0).toLocaleString('zh-CN');
}

export function isOwner() { return state.me && state.me.kind === 'owner'; }
export function can(perm) { return isOwner() || (state.me && state.me.perms.includes(perm)); }

// ---------- 表单小部件 ----------

export function field(label, input, hint) {
  return h('label', { class: 'field' }, h('span', { class: 'label' }, label), input, hint ? h('span', { class: 'hint' }, hint) : null);
}

export function select(options, value, attrs = {}) {
  const s = h('select', attrs);
  for (const o of options) {
    const [v, l] = Array.isArray(o) ? o : [o, o];
    s.append(h('option', { value: v, selected: String(v) === String(value) ? true : null }, l));
  }
  return s;
}

// 文件选择 + 上传列表
export function uploader({ multiple = true, accept = '', label = '选择文件', onChange, purpose = 'source' } = {}) {
  const files = [];
  const list = h('div', { class: 'uplist' });
  const input = h('input', { type: 'file', multiple: multiple || null, accept: accept || null, hidden: true });
  const drop = h('div', { class: 'drop', onclick: () => input.click() }, h('div', {}, label), h('div', { class: 'muted small' }, '点击选择，或把文件拖到这里'));
  const renderList = () => {
    clear(list, files.map((f, i) => h('div', { class: 'upitem' },
      h('span', { class: 'name' }, f.name), h('span', { class: 'muted small' }, f.status),
      h('div', { class: 'bar mini' }, h('i', { style: { width: `${Math.round((f.progress || 0) * 100)}%` } })),
      h('button', { class: 'icon-btn', title: '移除', onclick: (e) => { e.stopPropagation(); files.splice(i, 1); renderList(); onChange && onChange(files); } }, '×'))));
  };
  const add = async (fileList) => {
    for (const file of fileList) {
      const entry = { name: file.name, status: '上传中', progress: 0, record: null };
      if (!multiple) files.splice(0, files.length);
      files.push(entry);
      renderList();
      try {
        entry.record = await uploadFile(file, { purpose, onProgress: (p) => { entry.progress = p; renderList(); } });
        entry.status = fmtSize(entry.record.size);
        entry.progress = 1;
      } catch (e) {
        entry.status = '失败：' + e.message;
      }
      renderList();
      onChange && onChange(files);
    }
  };
  input.addEventListener('change', () => { add([...input.files]); input.value = ''; });
  drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', (e) => { e.preventDefault(); drop.classList.remove('over'); add([...e.dataTransfer.files]); });
  const el = h('div', { class: 'uploader' }, drop, input, list);
  el.ids = () => files.filter((f) => f.record).map((f) => f.record.id);
  el.records = () => files.filter((f) => f.record).map((f) => f.record);
  el.busy = () => files.some((f) => !f.record && f.status === '上传中');
  return el;
}

export function empty(text, action) {
  return h('div', { class: 'empty' }, h('p', {}, text), action || null);
}

export function tabs(items, active, onSelect) {
  return h('div', { class: 'tabs' }, items.map(([k, l]) => h('button', { class: 'tab' + (k === active ? ' active' : ''), onclick: () => onSelect(k) }, l)));
}
