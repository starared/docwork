import { api, clear, errToast, field, h, jobCard, modal, post, select, state, toast, uploader } from '../lib.js';

const TITLES = { ppt: 'AI 生成 PPT', doc: 'AI 生成 Word', xls: 'AI 生成 Excel' };
const INTENTS = [['cover', '封面'], ['toc', '目录'], ['section', '章节过渡'], ['points', '要点'], ['compare', '对比'], ['process', '流程'],
  ['timeline', '时间线'], ['matrix', '2x2 分析'], ['hierarchy', '层级'], ['data', '数据图表'], ['metrics', '关键指标'], ['table', '表格'],
  ['quote', '引用'], ['image', '图片'], ['team', '人物'], ['summary', '总结'], ['ending', '结束页']];

export async function render(page, kind) {
  const f = {};
  const src = uploader({ label: '上传资料（可选）：PDF、Word、PPT、Excel、CSV、文本、图片' });
  const topic = h('textarea', { rows: 4, placeholder: kind === 'xls' ? '例如：按地区和月份汇总上传的销售数据，找出增长最快的地区并画图' : kind === 'doc' ? '例如：根据上传的实验记录写一份实验报告，包括背景、方法、结果和结论' : '例如：2026 年度业务回顾与明年规划，面向公司管理层' });
  const form = h('div', { class: 'card' });
  const items = [field(kind === 'xls' ? '要求' : '主题或要求', topic), field('资料', src, kind === 'xls' ? '上传 CSV / Excel 时，由程序计算分析结果；不上传则由 AI 设计表格' : '资料只作为参考数据，内容中的数字都来自资料')];
  // 不常用的设置收进“高级选项”，第一屏只有主题和资料
  const adv = [];
  if (kind !== 'xls') {
    f.urls = h('textarea', { rows: 2, placeholder: '参考网页（可选），每行一个网址，最多 5 个' });
    adv.push(field('参考网页', f.urls, '读取网页正文作为资料；只能访问公网地址'));
    if (state.me && state.me.models && state.me.models.search) {
      f.web_search = h('input', { type: 'checkbox' });
      adv.push(h('label', { class: 'row gap', style: { marginBottom: '12px' } }, f.web_search, '联网检索资料（AI 根据题目搜索网页，引用处标注来源编号）'));
    }
  }
  if (kind === 'ppt') {
    f.pages = h('input', { type: 'number', min: 3, max: 60, placeholder: '自动（10–15 页）' });
    f.audience = h('input', { type: 'text', placeholder: '例如：公司管理层、学生、客户' });
    f.purpose = select([['', '不指定'], '工作汇报', '毕业答辩', '课堂授课', '融资路演', '产品介绍', '培训'], '');
    f.style = h('input', { type: 'text', placeholder: '例如：简洁商务、科技感、学术风格' });
    f.theme_preset = select([['', '自动选择']].concat(Object.keys((state.meta || {}).themes || {}).map((k) => [k, k])), '');
    f.aspect = select([['16:9', '16:9 宽屏'], ['4:3', '4:3']], '16:9');
    f.image_mode = select([['auto', '自动（图库优先，其次生成）'], ['stock', '只用图库'], ['generate', '只用图像生成'], ['none', '不配图']], 'auto');
    f.font_mode = select([['system', '微软雅黑等 Windows 自带字体'], ['open', '思源字体（需在电脑上安装）']], 'system');
    f.footer = h('input', { type: 'text', placeholder: '页脚文字（可选）' });
    f.language = select([['', '简体中文'], ['English', 'English'], ['繁體中文', '繁體中文'], ['日本語', '日本語']], '');
    f.confirm_outline = h('input', { type: 'checkbox', checked: true });
    f.logo = uploader({ multiple: false, accept: 'image/*', label: 'Logo（可选）' });
    f.themeFile = uploader({ multiple: false, accept: '.pptx', label: '参考 PPT（可选，只提取配色和字体）' });
    adv.push(h('div', { class: 'grid c3' },
      field('页数', f.pages), field('受众', f.audience), field('用途', f.purpose),
      field('风格偏好', f.style), field('主题', f.theme_preset), field('比例', f.aspect),
      field('配图', f.image_mode), field('字体', f.font_mode), field('语言', f.language)),
    h('div', { class: 'grid c2', style: { marginBottom: '12px' } }, f.logo, f.themeFile), field('页脚', f.footer));
  } else if (kind === 'doc') {
    f.preset = select([['', '自动判断']].concat(Object.entries((state.meta || {}).doc_presets || { report: '报告' })), '');
    f.pages = h('input', { type: 'number', min: 1, max: 100, placeholder: '自动' });
    f.audience = h('input', { type: 'text', placeholder: '例如：导师、评审专家、客户' });
    f.font_mode = select([['system', 'Windows 自带字体（宋体、微软雅黑、仿宋_GB2312 等）'], ['open', '思源字体（需在电脑上安装）']], 'system');
    f.image_mode = select([['auto', '自动'], ['stock', '只用图库'], ['generate', '只用图像生成'], ['none', '不配图']], 'none');
    f.header_text = h('input', { type: 'text', placeholder: '页眉文字（可选）' });
    f.language = select([['', '简体中文'], ['English', 'English']], '');
    f.confirm_outline = h('input', { type: 'checkbox', checked: true });
    f.logo = uploader({ multiple: false, accept: 'image/*', label: 'Logo（可选，放在页眉）' });
    adv.push(h('div', { class: 'grid c3' }, field('文档类型', f.preset), field('篇幅（页）', f.pages), field('读者', f.audience),
      field('字体', f.font_mode), field('配图', f.image_mode), field('语言', f.language)), field('页眉', f.header_text),
    h('div', { style: { marginBottom: '12px' } }, f.logo));
  }
  f.extra = h('textarea', { rows: 2, placeholder: '其他要求（可选）' });
  if (adv.length) {
    adv.push(field('其他要求', f.extra));
    const hint = kind === 'ppt' ? '页数、受众、主题、配图、字体、参考网页等' : '文档类型、篇幅、字体、配图、参考网页等';
    const det = h('details', { class: 'adv' }, h('summary', {}, '高级选项', h('span', { class: 'muted small' }, hint)), h('div', { class: 'adv-body' }, adv));
    try { det.open = localStorage.getItem('dw.create.adv') === '1'; } catch (e) { /* 忽略 */ }
    det.addEventListener('toggle', () => { try { localStorage.setItem('dw.create.adv', det.open ? '1' : '0'); } catch (e) { /* 忽略 */ } });
    items.push(det);
  } else {
    items.push(field('其他要求', f.extra));
  }
  const submit = h('button', { class: 'btn primary', onclick: () => go() }, '开始生成');
  const jobBox = h('div', { style: { marginTop: '14px' } });
  clear(form, items, h('div', { class: 'submit-bar row gap wrap' }, submit,
    f.confirm_outline ? h('label', { class: 'row gap small' }, f.confirm_outline, kind === 'ppt' ? '先生成大纲，确认后再继续' : '先生成文档结构，确认后再继续') : null,
    h('span', { class: 'muted small' }, '生成过程中可以离开本页，进度在任务中心查看')));
  clear(page, h('h1', {}, TITLES[kind]), form, jobBox);
  let stop = null;

  async function go() {
    if (src.busy() || (f.logo && f.logo.busy()) || (f.themeFile && f.themeFile.busy())) return toast('文件还在上传，请稍候');
    const p = { topic: topic.value.trim(), file_ids: src.ids(), extra: f.extra.value.trim() };
    for (const k of ['pages', 'audience', 'purpose', 'style', 'theme_preset', 'aspect', 'image_mode', 'font_mode', 'footer', 'language', 'preset', 'header_text']) {
      if (f[k] && f[k].value !== '') p[k] = f[k].type === 'number' ? Number(f[k].value) : f[k].value;
    }
    if (f.confirm_outline) p.confirm_outline = f.confirm_outline.checked;
    if (f.web_search && f.web_search.checked) p.web_search = true;
    if (f.urls) {
      const urls = f.urls.value.split(/\s+/).filter(Boolean);
      if (urls.length) p.urls = urls;
    }
    if (f.logo && f.logo.ids()[0]) p.logo_file_id = f.logo.ids()[0];
    if (f.themeFile && f.themeFile.ids()[0]) p.theme_file_id = f.themeFile.ids()[0];
    if (!p.topic && !p.file_ids.length && !p.urls) return toast('请填写主题、上传资料或给出参考网页', 'error');
    submit.disabled = true;
    try {
      const job = await post('/api/jobs', { kind: `gen_${kind}`, params: p });
      showJob(job);
    } catch (e) { errToast(e); submit.disabled = false; }
  }

  function showJob(job) {
    if (stop) stop();
    const c = jobCard(job, { onDone: (j) => {
      submit.disabled = false;
      if (j.status === 'awaiting_input') outlineDialog(j, kind, (nj) => showJob(nj));
      else if (j.status === 'done' && j.result && j.result.work_id) {
        toast('生成完成', 'ok');
        location.hash = `#/work/${j.result.work_id}`;
      }
    } });
    stop = c._stop;
    clear(jobBox, c);
  }
  return () => stop && stop();
}

export function outlineDialog(job, kind, onContinue) {
  const outline = JSON.parse(JSON.stringify(job.result.outline));
  const isPpt = !!outline.slides;
  const list = h('div', { class: 'outline-edit' });
  const title = h('input', { type: 'text', value: outline.title || '' });
  const arr = isPpt ? outline.slides : outline.sections;
  const draw = () => {
    clear(list, arr.map((s, i) => h('div', { class: 'sl' },
      h('span', { class: 'muted small', style: { width: '26px', paddingTop: '8px' } }, i + 1),
      isPpt ? select(INTENTS, s.intent, { style: { width: '110px' }, onchange: (e) => { s.intent = e.target.value; } })
        : select([[1, '一级'], [2, '二级'], [3, '三级']], s.level || 1, { style: { width: '80px' }, onchange: (e) => { s.level = Number(e.target.value); } }),
      h('div', { class: 'grow col', style: { gap: '4px' } },
        h('input', { type: 'text', value: isPpt ? s.title : s.heading, oninput: (e) => { if (isPpt) s.title = e.target.value; else s.heading = e.target.value; } }),
        h('textarea', { rows: 2, placeholder: '要点，每行一条', oninput: (e) => { s.points = e.target.value.split('\n').filter((x) => x.trim()); } }, (s.points || []).join('\n'))),
      h('div', { class: 'col', style: { gap: '2px' } },
        h('button', { class: 'icon-btn', title: '上移', onclick: () => { if (i > 0) { arr.splice(i - 1, 0, arr.splice(i, 1)[0]); draw(); } } }, '↑'),
        h('button', { class: 'icon-btn', title: '下移', onclick: () => { if (i < arr.length - 1) { arr.splice(i + 1, 0, arr.splice(i, 1)[0]); draw(); } } }, '↓'),
        h('button', { class: 'icon-btn', title: '删除', onclick: () => { arr.splice(i, 1); draw(); } }, '×')))));
  };
  draw();
  const addBtn = h('button', { class: 'btn small', onclick: () => { arr.push(isPpt ? { title: '新页面', intent: 'points', points: [] } : { heading: '新章节', level: 1, points: [] }); draw(); } }, isPpt ? '添加页面' : '添加章节');
  modal(isPpt ? '确认大纲' : '确认文档结构', h('div', {}, field('标题', title), list, addBtn), {
    wide: true,
    actions: [
      { label: '取消任务', kind: 'ghost', onClick: async () => { await post(`/api/jobs/${job.id}/cancel`); } },
      { label: '确认并继续生成', kind: 'primary', onClick: async () => {
        outline.title = title.value;
        const nj = await post(`/api/jobs/${job.id}/continue`, { outline });
        onContinue(nj);
      } },
    ],
  });
}
