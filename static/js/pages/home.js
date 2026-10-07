import { api, can, clear, empty, fileUrl, fmtTime, h, isOwner, jobCard, skeleton, state } from '../lib.js';
import { workCard } from './works.js';

export async function render(page) {
  const me = state.me;
  const cards = [
    ['ppt', 'PPT', 'AI 生成 PPT', '根据主题或资料生成大纲、版式、图表、配图和演讲备注'],
    ['doc', 'W', 'AI 生成 Word', '报告、方案、论文、纪要、说明书、公文，自动目录与公式'],
    ['xls', 'X', 'AI 生成 Excel', '分析上传的数据或设计表格，真实公式并自动验证'],
  ].filter(([k]) => can(k));
  const tools = [
    ['#/tools/import', '导入已有文件', '修改已有 PPT / Word / Excel，保留原版式', 'import'],
    ['#/tools/convert', '格式转换', 'Office 转 PDF、PDF 转 Word 等', 'convert'],
    ['#/tools/pdf', 'PDF 工具', '合并、拆分、压缩、旋转、排序、提取', 'pdf'],
    ['#/tools/ocr', '扫描件 OCR', '生成可搜索 PDF 或可编辑文字', 'pdf'],
  ].filter((t) => can(t[3]));
  const warn = !me.models.text
    ? h('div', { class: 'card', style: { borderColor: 'var(--warn)' } },
      isOwner() ? ['尚未配置可用的文字模型接口，AI 功能暂不可用。', h('a', { href: '#/admin/models' }, ' 去配置')] : '管理员尚未配置模型接口，AI 功能暂不可用；格式转换和 PDF 工具可以正常使用。')
    : null;
  const jobsBox = h('div', {}, skeleton('rows', 1));
  const worksBox = h('div', { class: 'works' }, [...skeleton('cards', 4).children]);
  clear(page,
    h('h1', {}, isOwner() ? '工作台' : `欢迎，${me.note || '访客'}`),
    warn,
    cards.length ? h('div', { class: 'grid c3', style: { marginTop: '10px' } }, cards.map(([k, ico, t, d]) =>
      h('div', { class: 'card create-card', onclick: () => { location.hash = `#/create/${k}`; } }, h('div', { class: 'ico' }, ico), h('h3', {}, t), h('p', { class: 'muted small' }, d)))) : null,
    tools.length ? h('div', { class: 'grid c4', style: { marginTop: '14px' } }, tools.map(([href, t, d]) =>
      h('a', { class: 'card create-card', href, style: { color: 'inherit', textDecoration: 'none' } }, h('h3', {}, t), h('p', { class: 'muted small' }, d)))) : null,
    !isOwner() && me.quota && Object.keys(me.quota).length ? quotaCard(me) : null,
    h('h2', { style: { marginTop: '22px' } }, '进行中的任务'), jobsBox,
    h('div', { class: 'row between', style: { marginTop: '22px' } }, h('h2', {}, '最近的作品'), h('a', { href: '#/works' }, '全部作品')), worksBox,
  );
  const stops = [];
  const [jobs, works] = await Promise.all([api('/api/jobs?status=active&limit=10'), api('/api/works?limit=8')]);
  clear(jobsBox);
  clear(worksBox);
  if (!jobs.items.length) jobsBox.append(h('p', { class: 'muted' }, '没有进行中的任务'));
  for (const j of jobs.items) {
    const c = jobCard(j, { onDone: (jj) => { if (jj.status === 'done' && jj.result && jj.result.work_id) c.append(h('a', { href: `#/work/${jj.result.work_id}` }, '打开作品')); } });
    stops.push(c._stop);
    jobsBox.append(c);
  }
  if (!works.items.length) {
    worksBox.replaceWith(cards.length ? empty('还没有作品。', [cards[0][2], `#/create/${cards[0][0]}`])
      : empty('还没有作品。', can('import') ? ['导入已有文件', '#/tools/import'] : null));
  }
  for (const w of works.items) worksBox.append(workCard(w));
  return () => stops.forEach((s) => s());
}

function quotaCard(me) {
  const q = me.quota, u = me.usage || {};
  const rows = [];
  if (q.gen_count !== undefined) rows.push(['生成次数', `${u.gen || 0} / ${q.gen_count}`]);
  if (q.tokens !== undefined) rows.push(['模型用量（token）', `${(u.tokens || 0).toLocaleString()} / ${Number(q.tokens).toLocaleString()}`]);
  if (q.storage_mb !== undefined) rows.push(['存储', `${((u.storage_bytes || 0) / 1024 / 1024).toFixed(1)} / ${q.storage_mb} MB`]);
  if (q.concurrent !== undefined) rows.push(['同时运行的任务', `${u.running || 0} / ${q.concurrent}`]);
  if (q.upload_mb !== undefined) rows.push(['单文件上传上限', `${q.upload_mb} MB`]);
  return h('div', { class: 'card', style: { marginTop: '14px' } }, h('h3', {}, '我的额度'),
    h('div', { class: 'kv', style: { marginTop: '8px' } }, rows.map(([k, v]) => [h('span', { class: 'muted' }, k), h('span', {}, v)])));
}
