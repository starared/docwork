import { api, clear, download, errToast, field, fileUrl, fmtSize, h, jobCard, post, select, state, tabs, toast, uploader } from '../lib.js';
import { reportDialog } from './jobs.js';

const TITLES = { convert: '格式转换', pdf: 'PDF 工具', ocr: '扫描件 OCR', import: '导入已有文件' };

export async function render(page, tool) {
  const box = h('div');
  clear(page, h('h1', {}, TITLES[tool]), box);
  const stops = [];
  const runJob = async (kind, params, out) => {
    try {
      const job = await post('/api/jobs', { kind, params });
      const c = jobCard(job, { onDone: (j) => showResult(j, out) });
      stops.push(c._stop);
      clear(out, c);
    } catch (e) { errToast(e); }
  };
  if (tool === 'convert') convertTool(box, runJob);
  else if (tool === 'pdf') pdfTool(box, runJob);
  else if (tool === 'ocr') ocrTool(box, runJob);
  else importTool(box, runJob);
  return () => stops.forEach((s) => s());
}

function showResult(j, out) {
  const r = j.result || {};
  if (j.status !== 'done') return;
  const files = r.files || [];
  out.append(h('div', { class: 'card', style: { marginTop: '10px' } },
    h('strong', {}, '结果'),
    files.map((f) => h('div', { class: 'row gap', style: { marginTop: '6px' } }, h('span', { class: 'grow' }, `${f.name}（${fmtSize(f.size)}）`),
      f.mime && (f.mime.startsWith('image/') || f.mime === 'application/pdf') ? h('a', { class: 'btn small ghost', href: fileUrl(f.file_id, true), target: '_blank', rel: 'noopener' }, '预览') : null,
      h('button', { class: 'btn small primary', onclick: () => download(f.file_id) }, '下载'),
      h('button', { class: 'btn small ghost', onclick: async (e) => { await post(`/api/files/${f.file_id}/keep`); e.target.textContent = '已保存'; e.target.disabled = true; } }, '保存到我的文件'))),
    r.work_id ? h('a', { class: 'btn small primary', href: `#/work/${r.work_id}`, style: { marginTop: '8px' } }, '打开作品') : null,
    r.report ? h('div', { style: { marginTop: '8px' } }, reportLine(r.report), h('button', { class: 'btn small ghost', onclick: () => reportDialog(r.report) }, '完整报告')) : null,
    h('p', { class: 'muted small', style: { marginTop: '8px' } }, '工具结果默认保留 24 小时；需要长期保存请点“保存到我的文件”。')));
}

function reportLine(r) {
  const parts = [];
  if (r.fidelity) parts.push(`保真等级：${r.fidelity}`);
  if (r.engine) parts.push(`引擎：${r.engine}`);
  if (r.ocr_pages && r.ocr_pages.length) parts.push(`OCR 识别 ${r.ocr_pages.length} 页`);
  if (r.low_confidence && r.low_confidence.length) parts.push(`${r.low_confidence.length} 处低置信度文字`);
  if (r.before) parts.push(`${fmtSize(r.before)} → ${fmtSize(r.after)}`);
  if (r.notes && r.notes.length) parts.push(r.notes.join('；'));
  return h('div', { class: 'small' }, parts.join('　'));
}

function convertTool(box, runJob) {
  const targets = h('div');
  let lastId = null;
  const up = uploader({ multiple: false, label: '选择要转换的文件', onChange: () => {
    const rec = up.records()[0];
    if (!rec) { lastId = null; clear(targets); return; }
    if (rec.id !== lastId) { lastId = rec.id; refresh(); }
  } });
  const out = h('div');
  const matrix = (state.meta && state.meta.conversions) || [];
  const refresh = async () => {
    const rec = up.records()[0];
    if (!rec) return clear(targets);
    const kind = rec.meta.kind;
    const r = await api(`/api/convert/targets?kind=${encodeURIComponent(kind)}`);
    clear(targets, r.targets.length ? h('div', { class: 'col' }, h('strong', {}, `把 ${rec.name} 转换为：`),
      r.targets.map((t) => h('div', { class: 'row gap card', style: { padding: '10px' } },
        h('div', { class: 'grow' }, h('strong', {}, t.label), h('div', { class: 'muted small' }, `${t.tool} · 保真等级：${t.fidelity}`)),
        t.ai ? h('a', { class: 'btn small', href: '#/tools/import' }, '使用重建功能')
          : h('button', { class: 'btn small primary', onclick: () => runJob('convert', { file_id: rec.id, target: t.target }, out) }, '转换')))) : h('p', { class: 'muted' }, '该文件类型没有可用的转换'));
  };
  clear(box, h('div', { class: 'card' }, up, targets), out,
    h('details', { class: 'card', style: { marginTop: '14px' } }, h('summary', {}, '支持的转换与保真等级'),
      h('table', { class: 't', style: { marginTop: '8px' } }, h('tr', {}, h('th', {}, '输入'), h('th', {}, '输出'), h('th', {}, '工具'), h('th', {}, '保真等级')),
        matrix.map((m) => h('tr', {}, h('td', {}, m.inputs.join(', ')), h('td', {}, m.target), h('td', {}, m.tool), h('td', {}, m.fidelity))))));
}

function pdfTool(box, runJob) {
  let op = 'merge';
  const body = h('div');
  const out = h('div');
  const draw = () => {
    clear(box, tabs([['merge', '合并'], ['split', '拆分'], ['compress', '压缩'], ['rotate', '旋转'], ['reorder', '排序与删页'], ['extract_text', '提取文字'], ['extract_images', '提取图片']], op, (k) => { op = k; draw(); }), body, out);
    clear(out);
    let onUp = () => {};
    const up = uploader({ multiple: op === 'merge', accept: '.pdf,application/pdf', label: op === 'merge' ? '选择要合并的 PDF（按列表顺序合并）' : '选择 PDF', onChange: () => onUp() });
    const opts = h('div');
    let getOpts = () => ({});
    if (op === 'merge') {
      const bm = h('input', { type: 'checkbox', checked: true });
      opts.append(h('label', { class: 'row gap' }, bm, '保留书签，并为每个文件添加一个书签'));
      getOpts = () => ({ bookmarks: bm.checked });
    } else if (op === 'split') {
      const mode = select([['ranges', '按页码范围'], ['every', '每 N 页一份'], ['bookmarks', '按顶级书签']], 'ranges');
      const ranges = h('input', { type: 'text', placeholder: '例如：1-3, 4-8, 9-' });
      const every = h('input', { type: 'number', min: 1, value: 1 });
      opts.append(field('方式', mode), field('页码范围', ranges, '每段一份，用逗号分隔'), field('每份页数', every));
      getOpts = () => ({ mode: mode.value, ranges: ranges.value, every: Number(every.value) });
    } else if (op === 'compress') {
      const level = select([['screen', '最小（适合屏幕浏览）'], ['ebook', '平衡（推荐）'], ['printer', '高质量（适合打印）']], 'ebook');
      opts.append(field('压缩档位', level));
      getOpts = () => ({ level: level.value });
    } else if (op === 'extract_text') {
      const fmt = select([['md', 'Markdown（保留表格结构）'], ['txt', '纯文本']], 'md');
      opts.append(field('格式', fmt), h('p', { class: 'muted small' }, '扫描页会自动 OCR 识别。'));
      getOpts = () => ({ format: fmt.value });
    } else if (op === 'rotate' || op === 'reorder') {
      opts.append(h('p', { class: 'muted small' }, '上传后显示页面缩略图。'));
    }
    const pagesBox = h('div');
    let pageState = null;
    if (op === 'rotate' || op === 'reorder') {
      const loadPages = async (rec) => {
        clear(pagesBox, h('p', { class: 'muted' }, '正在生成缩略图…'));
        const job = await post('/api/jobs', { kind: 'file_pages', params: { file_id: rec.id } });
        const c = jobCard(job, { compact: true, onDone: (j) => {
          if (j.status !== 'done') return;
          pageState = j.result.pages.map((p, i) => ({ n: i + 1, fid: p.file_id, rot: 0, on: true }));
          drawPages();
        } });
        clear(pagesBox, c);
      };
      const drawPages = () => {
        const grid = h('div', { class: 'pagegrid' });
        let dragIdx = null;
        pageState.forEach((p, i) => {
          const img = h('img', { src: fileUrl(p.fid, true), style: { transform: `rotate(${p.rot}deg)` } });
          grid.append(h('div', { class: 'pg' + (p.on ? '' : ' off'), draggable: op === 'reorder' ? 'true' : null,
            ondragstart: () => { dragIdx = i; }, ondragover: (e) => e.preventDefault(),
            ondrop: () => { if (dragIdx === null) return; const [x] = pageState.splice(dragIdx, 1); pageState.splice(i, 0, x); drawPages(); },
            onclick: () => { if (op === 'rotate') p.rot = (p.rot + 90) % 360; else p.on = !p.on; drawPages(); } },
          img, h('div', {}, `第 ${p.n} 页${op === 'rotate' && p.rot ? ` · ${p.rot}°` : ''}`)));
        });
        clear(pagesBox, h('p', { class: 'muted small' }, op === 'rotate' ? '点击页面每次旋转 90°' : '拖动调整顺序，点击页面切换保留/删除'), grid);
      };
      onUp = () => { const r = up.records()[0]; if (r && pagesBox.dataset.fid !== r.id) { pagesBox.dataset.fid = r.id; loadPages(r); } };
    }
    const run = h('button', { class: 'btn primary', onclick: async () => {
      const ids = up.ids();
      if (!ids.length) return toast('请先上传 PDF');
      if (op === 'rotate' || op === 'reorder') {
        if (!pageState) return toast('缩略图还在生成');
        if (op === 'reorder') return runJob('pdf_tool', { op: 'reorder', file_ids: ids, options: { order: pageState.filter((p) => p.on).map((p) => p.n) } }, out);
        // 旋转：按角度分组依次执行
        const groups = {};
        for (const p of pageState) if (p.rot) (groups[p.rot] = groups[p.rot] || []).push(p.n);
        const entries = Object.entries(groups);
        if (!entries.length) return toast('没有需要旋转的页面');
        if (entries.length > 1) return toast('一次只能按同一个角度旋转，请分次操作', 'error');
        return runJob('pdf_tool', { op: 'rotate', file_ids: ids, options: { pages: entries[0][1].join(','), angle: Number(entries[0][0]) } }, out);
      }
      runJob('pdf_tool', { op, file_ids: ids, options: getOpts() }, out);
    } }, '开始处理');
    clear(body, h('div', { class: 'card' }, up, opts, pagesBox, run));
  };
  draw();
}

function ocrTool(box, runJob) {
  const up = uploader({ multiple: false, accept: '.pdf,image/*', label: '选择扫描 PDF 或图片' });
  const output = select([['pdf', '可搜索 PDF（保留原样，添加隐藏文字层）'], ['txt', '文字（Markdown，含识别出的表格）'], ['docx', 'Word（按阅读顺序重新排版）']], 'pdf');
  const out = h('div');
  clear(box, h('div', { class: 'card' }, up, field('输出', output),
    h('p', { class: 'muted small' }, '识别文字、推断表格、恢复多栏阅读顺序是三个独立步骤；结果附带报告，列出低置信度文字和可能错乱的位置。'),
    h('button', { class: 'btn primary', onclick: () => { const id = up.ids()[0]; if (!id) return toast('请先上传文件'); runJob('ocr', { file_id: id, output: output.value }, out); } }, '开始识别')), out);
}

function importTool(box, runJob) {
  const up = uploader({ multiple: false, label: '选择 PPT、Word、Excel、PDF 或图片' });
  const mode = select([['inplace', '原位修改：保留原版式、母版和动画，只改内容'], ['rebuild_ppt', '重建为演示文稿：提取内容后按本系统布局重新设计'], ['rebuild_doc', '重建为 Word：提取内容后按本系统样式重新排版']], 'inplace');
  const instr = h('textarea', { rows: 2, placeholder: '重建时的补充要求（可选）' });
  const out = h('div');
  clear(box, h('div', { class: 'card' }, up, field('方式', mode, 'PDF 和图片只能重建；旧格式（doc、ppt、xls）会先转换为新格式'), field('补充要求', instr),
    h('button', { class: 'btn primary', onclick: () => {
      const id = up.ids()[0];
      if (!id) return toast('请先上传文件');
      if (mode.value === 'inplace') runJob('import_file', { file_id: id }, out);
      else runJob('rebuild', { file_id: id, target: mode.value === 'rebuild_ppt' ? 'ppt' : 'doc', instruction: instr.value }, out);
    } }, '开始')), out);
}
