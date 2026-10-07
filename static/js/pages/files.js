import { api, can, clear, del, download, empty, fileUrl, fmtDateTime, fmtSize, h, post, skeleton, tabs, toast } from '../lib.js';

const EMPTY = {
  saved: ['还没有保存的文件。工具结果默认临时保存，点“保存”后会出现在这里。', '格式转换', '#/tools/convert', 'convert'],
  output: ['没有工具结果。格式转换、PDF 工具和 OCR 的结果会临时保存在这里。', '格式转换', '#/tools/convert', 'convert'],
  upload: ['没有上传的资料。生成文档时上传的资料会临时保存在这里。', 'AI 生成 PPT', '#/create/ppt', 'ppt'],
};

export async function render(page) {
  let kind = 'saved';
  const list = h('div');
  const tabBox = h('div');
  const draw = async () => {
    clear(tabBox, tabs([['saved', '已保存的文件'], ['output', '工具结果（临时）'], ['upload', '上传的资料（临时）']], kind, (k) => { kind = k; draw(); }));
    clear(list, skeleton('rows', 5));
    const r = await api(`/api/files?kind=${kind}&limit=200`);
    if (!r.items.length) {
      const [text, label, href, perm] = EMPTY[kind];
      return clear(list, empty(text, can(perm) ? [label, href] : null));
    }
    // 手机上（窄屏）表格按行显示为卡片，data-label 是卡片里每项前面的列名
    clear(list, h('table', { class: 't stack' }, h('tr', {}, h('th', {}, '名称'), h('th', {}, '大小'), h('th', {}, '时间'), h('th', {}, kind === 'saved' ? '' : '过期时间'), h('th', {}, '')),
      r.items.map((f) => h('tr', {},
        h('td', { class: 'name' }, f.name), h('td', { 'data-label': '大小' }, fmtSize(f.size)), h('td', { class: 'small', 'data-label': '时间' }, fmtDateTime(f.created_at)),
        h('td', { class: 'small muted', 'data-label': f.expires_at ? '过期' : '' }, f.expires_at ? fmtDateTime(f.expires_at) : ''),
        h('td', { class: 'row gap actions' },
          f.mime.startsWith('image/') || f.mime === 'application/pdf' ? h('a', { class: 'btn small ghost', href: fileUrl(f.id, true), target: '_blank', rel: 'noopener' }, '预览') : null,
          h('button', { class: 'btn small', onclick: () => download(f.id) }, '下载'),
          kind !== 'saved' ? h('button', { class: 'btn small ghost', onclick: async () => { await post(`/api/files/${f.id}/keep`); toast('已保存'); draw(); } }, '保存') : null,
          h('button', { class: 'btn small ghost', onclick: async () => { await del(`/api/files/${f.id}`); draw(); } }, '删除'))))));
  };
  clear(page, h('h1', {}, '我的文件'), tabBox, list);
  await draw();
}
