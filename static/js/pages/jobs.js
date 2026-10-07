import { api, clear, download, empty, fmtDateTime, fmtSize, h, isOwner, JOB_STATUS, jobCard, modal, post, select, skeleton } from '../lib.js';
import { outlineDialog } from './create.js';

export async function render(page) {
  const st = { status: '', all: false };
  const list = h('div');
  const statusSel = select([['', '全部状态'], ['active', '进行中'], ['done', '完成'], ['failed', '失败'], ['cancelled', '已取消']], '', { style: { maxWidth: '150px' } });
  const allChk = h('input', { type: 'checkbox' });
  statusSel.addEventListener('change', () => { st.status = statusSel.value; load(); });
  allChk.addEventListener('change', () => { st.all = allChk.checked; load(); });
  clear(page, h('h1', {}, '任务中心'),
    h('div', { class: 'row gap', style: { marginBottom: '12px' } }, statusSel, isOwner() ? h('label', { class: 'row gap small' }, allChk, '显示所有工作区') : null,
      h('button', { class: 'btn small', onclick: () => load() }, '刷新')), list);
  let stops = [];
  async function load() {
    stops.forEach((s) => s());
    stops = [];
    const qs = new URLSearchParams({ limit: 60 });
    if (st.status) qs.set('status', st.status);
    if (st.all) qs.set('all', '1');
    if (!list.firstChild) list.append(skeleton('rows', 4));
    const r = await api('/api/jobs?' + qs);
    clear(list);
    if (!r.items.length) {
      list.append(st.status
        ? empty('没有符合条件的任务', ['显示全部任务', () => { statusSel.value = ''; st.status = ''; load(); }])
        : empty('还没有任务。生成文档、导入文件或使用工具后，进度会显示在这里。', ['去工作台', '#/']));
    }
    for (const j of r.items) {
      const card = jobCard(j, { onDone: (jj, el) => extras(jj, el) });
      card.append(h('div', { class: 'muted small' }, `创建于 ${fmtDateTime(j.created_at)}${j.finished_at ? '，结束于 ' + fmtDateTime(j.finished_at) : ''}`));
      stops.push(card._stop);
      list.append(card);
    }
  }
  function extras(j, el) {
    const r = j.result || {};
    const box = h('div', { class: 'row gap wrap', style: { marginTop: '6px' } });
    if (j.status === 'awaiting_input' && r.outline) box.append(h('button', { class: 'btn small primary', onclick: () => outlineDialog(j, '', () => load()) }, '确认大纲'));
    if (r.work_id) box.append(h('a', { class: 'btn small', href: `#/work/${r.work_id}` }, '打开作品'));
    for (const f of r.files || []) box.append(h('button', { class: 'btn small', onclick: () => download(f.file_id) }, `下载 ${f.name}（${fmtSize(f.size)}）`));
    if (r.report && Object.keys(r.report).length) box.append(h('button', { class: 'btn small ghost', onclick: () => reportDialog(r.report) }, '查看报告'));
    if (r.reply) box.append(h('span', { class: 'small' }, r.reply));
    if (r.coverage !== undefined) box.append(h('span', { class: 'small muted' }, `文字覆盖率 ${r.coverage}%`));
    el.append(box);
  }
  await load();
  return () => stops.forEach((s) => s());
}

export function reportDialog(rep) {
  const rows = [];
  const labels = { tool: '工具', fidelity: '保真等级', engine: '引擎', pages: '页数', ocr_pages: 'OCR 识别的页', tables: '识别到的表格', images: '图片',
    before: '原大小', after: '压缩后', ratio: '比例', method: '方式', parts: '拆分份数', notes: '说明', low_confidence: '低置信度文字', editable: '可修改', preserved: '原样保留', summary: '摘要' };
  for (const [k, v] of Object.entries(rep)) {
    if (v === null || v === undefined || (Array.isArray(v) && !v.length)) continue;
    let val;
    if (k === 'before' || k === 'after') val = fmtSize(v);
    else if (k === 'low_confidence') val = h('div', {}, v.slice(0, 30).map((x) => h('div', { class: 'small' }, `第 ${x.page} 页：“${x.text}”（置信度 ${x.conf}）`)));
    else if (k === 'tables' && Array.isArray(v)) val = v.map((x) => `第 ${x.page} 页 ${x.count} 个`).join('，');
    else if (Array.isArray(v)) val = v.join('，');
    else if (typeof v === 'object') val = JSON.stringify(v);
    else val = String(v);
    rows.push(h('span', { class: 'muted' }, labels[k] || k), h('div', {}, val));
  }
  modal('处理报告', h('div', { class: 'kv' }, rows), { wide: true });
}
