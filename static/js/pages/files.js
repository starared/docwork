import { api, clear, del, download, empty, fileUrl, fmtDateTime, fmtSize, h, post, tabs, toast } from '../lib.js';

export async function render(page) {
  let kind = 'saved';
  const list = h('div');
  const tabBox = h('div');
  const draw = async () => {
    clear(tabBox, tabs([['saved', '已保存的文件'], ['output', '工具结果（临时）'], ['upload', '上传的资料（临时）']], kind, (k) => { kind = k; draw(); }));
    const r = await api(`/api/files?kind=${kind}&limit=200`);
    if (!r.items.length) return clear(list, empty('没有文件'));
    clear(list, h('table', { class: 't' }, h('tr', {}, h('th', {}, '名称'), h('th', {}, '大小'), h('th', {}, '时间'), h('th', {}, kind === 'saved' ? '' : '过期时间'), h('th', {}, '')),
      r.items.map((f) => h('tr', {},
        h('td', {}, f.name), h('td', {}, fmtSize(f.size)), h('td', { class: 'small' }, fmtDateTime(f.created_at)),
        h('td', { class: 'small muted' }, f.expires_at ? fmtDateTime(f.expires_at) : ''),
        h('td', { class: 'row gap' },
          f.mime.startsWith('image/') || f.mime === 'application/pdf' ? h('a', { class: 'btn small ghost', href: fileUrl(f.id, true), target: '_blank', rel: 'noopener' }, '预览') : null,
          h('button', { class: 'btn small', onclick: () => download(f.id) }, '下载'),
          kind !== 'saved' ? h('button', { class: 'btn small ghost', onclick: async () => { await post(`/api/files/${f.id}/keep`); toast('已保存'); draw(); } }, '保存') : null,
          h('button', { class: 'btn small ghost', onclick: async () => { await del(`/api/files/${f.id}`); draw(); } }, '删除'))))));
  };
  clear(page, h('h1', {}, '我的文件'), tabBox, list);
  await draw();
}
