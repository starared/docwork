import { api, clear, confirmDialog, csrfToken, del, empty, errToast, fileUrl, fmtTime, h, isOwner, patch, post, select, tabs, toast } from '../lib.js';

const KIND_ICON = { ppt: 'PPT', doc: 'W', xls: 'X', import_pptx: 'PPT', import_docx: 'W', import_xlsx: 'X' };

export function workCard(w, { selectable = false, selected, onToggle, trash = false, onChanged } = {}) {
  const thumb = h('div', { class: 'thumb' }, w.thumb ? null : KIND_ICON[w.kind] || '?');
  if (w.thumb) thumb.style.backgroundImage = `url(${fileUrl(w.thumb, true)})`;
  const el = h('div', { class: 'work', onclick: () => { if (!trash) location.hash = `#/work/${w.id}`; } },
    thumb,
    selectable ? h('input', { type: 'checkbox', class: 'sel', checked: selected, onclick: (e) => { e.stopPropagation(); onToggle && onToggle(e.target.checked); } }) : null,
    w.starred ? h('span', { class: 'star' }, '★') : null,
    h('div', { class: 'info' },
      h('div', { class: 't1', title: w.title }, w.title),
      h('div', { class: 'row between muted small' },
        h('span', {}, `${w.kind_label}${w.version ? ' · 第' + w.version + '版' : ''}${w.readonly ? ' · 只读分享' : ''}`),
        h('span', {}, fmtTime(w.updated_at))),
      w.issues ? h('div', { class: 'warn small' }, `仍有 ${w.issues} 处排版问题`) : null,
      trash ? h('div', { class: 'row gap', style: { marginTop: '6px' } },
        h('button', { class: 'btn small', onclick: async (e) => { e.stopPropagation(); await post(`/api/works/${w.id}/untrash`); toast('已恢复'); onChanged && onChanged(); } }, '恢复'),
        h('button', { class: 'btn small danger', onclick: async (e) => {
          e.stopPropagation();
          if (await confirmDialog('彻底删除', `彻底删除“${w.title}”？此操作不能撤销。`, '删除', true)) {
            await del(`/api/works/${w.id}?hard=1`); onChanged && onChanged();
          }
        } }, '彻底删除')) : null));
  return el;
}

export async function render(page) {
  const st = { kind: '', q: '', folder: '', tag: '', starred: false, trash: false, sort: 'updated', offset: 0, all: false };
  const selected = new Map();  // id → 列表项（打包下载直接用列表里的导出文件信息）
  const q = h('input', { type: 'search', placeholder: '搜索标题和正文', style: { maxWidth: '260px' } });
  const folderSel = h('select', { style: { maxWidth: '160px' } });
  const tagSel = h('select', { style: { maxWidth: '160px' } });
  const sortSel = select([['updated', '最近修改'], ['created', '最近创建'], ['title', '标题']], 'updated', { style: { maxWidth: '130px' } });
  const starChk = h('input', { type: 'checkbox' });
  const allChk = h('input', { type: 'checkbox' });
  const grid = h('div', { class: 'works' });
  const more = h('div', { style: { textAlign: 'center', margin: '16px' } });
  const bulk = h('div', { class: 'row gap' });
  const kindTabs = h('div');
  let timer = null;
  q.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => { st.q = q.value.trim(); load(true); }, 350); });
  folderSel.addEventListener('change', () => { st.folder = folderSel.value; load(true); });
  tagSel.addEventListener('change', () => { st.tag = tagSel.value; load(true); });
  sortSel.addEventListener('change', () => { st.sort = sortSel.value; load(true); });
  starChk.addEventListener('change', () => { st.starred = starChk.checked; load(true); });
  allChk.addEventListener('change', () => { st.all = allChk.checked; load(true); });
  const drawTabs = () => clear(kindTabs, tabs([['', '全部'], ['ppt', 'PPT'], ['doc', 'Word'], ['xls', 'Excel'], ['import', '导入的文件'], ['trash', '回收站']],
    st.trash ? 'trash' : st.kind, (k) => { st.trash = k === 'trash'; st.kind = k === 'trash' ? '' : k; drawTabs(); load(true); }));
  drawTabs();
  clear(page, h('h1', {}, '作品库'), kindTabs,
    h('div', { class: 'row gap wrap', style: { marginBottom: '14px' } }, q, folderSel, tagSel, sortSel,
      h('label', { class: 'row gap small' }, starChk, '只看星标'),
      isOwner() ? h('label', { class: 'row gap small' }, allChk, '显示所有工作区') : null,
      h('span', { class: 'grow' }), bulk),
    grid, more);

  async function load(reset) {
    if (reset) { st.offset = 0; selected.clear(); }
    const qs = new URLSearchParams({ limit: 40, offset: st.offset, sort: st.sort });
    if (st.kind) qs.set('kind', st.kind);
    if (st.q) qs.set('q', st.q);
    if (st.folder) qs.set('folder', st.folder);
    if (st.tag) qs.set('tag', st.tag);
    if (st.starred) qs.set('starred', '1');
    if (st.trash) qs.set('trash', '1');
    if (st.all) qs.set('all', '1');
    let r;
    try { r = await api('/api/works?' + qs); } catch (e) { return errToast(e); }
    if (reset) {
      clear(grid);
      clear(folderSel, h('option', { value: '' }, '全部文件夹'), r.folders.map((f) => h('option', { value: f, selected: f === st.folder || null }, f)));
      clear(tagSel, h('option', { value: '' }, '全部标签'), r.tags.map((t) => h('option', { value: t, selected: t === st.tag || null }, t)));
    }
    if (!r.items.length && reset) grid.append(empty(st.trash ? '回收站是空的' : '没有找到作品'));
    for (const w of r.items) {
      grid.append(workCard(w, { selectable: !st.trash, selected: selected.has(w.id), trash: st.trash, onChanged: () => load(true),
        onToggle: (on) => { if (on) selected.set(w.id, w); else selected.delete(w.id); drawBulk(); } }));
    }
    st.offset += r.items.length;
    clear(more, st.offset < r.total ? h('button', { class: 'btn', onclick: () => load(false) }, `加载更多（共 ${r.total} 个）`) : null);
    drawBulk();
  }

  function drawBulk() {
    clear(bulk, selected.size ? [
      h('span', { class: 'muted small' }, `已选 ${selected.size} 个`),
      h('button', { class: 'btn small', onclick: bulkDownload }, '打包下载'),
      h('button', { class: 'btn small', onclick: bulkFolder }, '移到文件夹'),
      h('button', { class: 'btn small danger', onclick: bulkTrash }, '删除'),
    ] : null);
  }

  async function bulkDownload() {
    const ids = [];
    for (const w of selected.values()) {
      const ex = w.exports || {};
      const main = ex.pptx || ex.docx || ex.xlsx || ex.pdf;
      if (main && main.file_id) ids.push(main.file_id);
    }
    if (!ids.length) return toast('所选作品没有可下载的文件');
    const r = await fetch('/api/files/zip', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken() }, body: JSON.stringify({ file_ids: ids }) });
    if (!r.ok) return toast('打包失败', 'error');
    const blob = await r.blob();
    const a = h('a', { href: URL.createObjectURL(blob), download: '作品打包.zip' });
    document.body.append(a); a.click(); a.remove();
  }

  async function bulkFolder() {
    const name = prompt('文件夹名称（留空表示移出文件夹）', st.folder || '');
    if (name === null) return;
    for (const wid of selected.keys()) await patch(`/api/works/${wid}`, { folder: name });
    toast('已移动');
    load(true);
  }

  async function bulkTrash() {
    if (!(await confirmDialog('删除作品', `把选中的 ${selected.size} 个作品移到回收站？回收站中的作品保留 30 天。`, '删除', true))) return;
    for (const wid of selected.keys()) await del(`/api/works/${wid}`);
    toast('已移到回收站');
    load(true);
  }

  await load(true);
}
