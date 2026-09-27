// 编辑器：预览、点选、对话修改、直接编辑、版本历史与对比、导出。
import { api, clear, confirmDialog, del, download, errToast, field, fileUrl, fmtDateTime, fmtTime, h, isOwner, jobCard, modal, patch, post, select, state, toast, watchJob } from '../lib.js';

const EXPORT_LABELS = { pptx: 'PPTX', docx: 'Word', xlsx: 'Excel', pdf: 'PDF', data_xlsx: '图表数据' };
// 页面预览图；历史版本的预览被清理后显示占位，后台重新生成完成后自动刷新
const pageImg = (p, alt = '', lazy = true) => (p && p.file_id
  ? h('img', { src: fileUrl(p.file_id, true), loading: lazy ? 'lazy' : null, alt })
  : h('div', { class: 'page-pending' }, '预览重新生成中…'));

const SOURCE_LABELS = { generate: '生成', chat: '对话修改', manual: '直接编辑', import: '导入', restore: '恢复', inplace: '原位修改', rebuild: '重建' };

export async function render(page, workId) {
  const S = {
    work: null, version: null, viewingOld: false, active: null,
    selSlides: new Set(), selElems: new Set(), selBlocks: new Set(), range: null, sheet: null,
    job: null, stopJob: null, history: [], stopPrev: null,
  };
  const root = h('div', { class: 'editor' });
  clear(page, root);
  let dragStart = null;
  const onUp = () => { dragStart = null; };
  document.addEventListener('mouseup', onUp);

  async function load(versionId) {
    const w = await api(`/api/works/${workId}${versionId ? '?version=' + versionId : ''}`);
    S.work = w;
    S.version = w.version;
    S.viewingOld = !!(w.version && w.current_version_id && w.version.id !== w.current_version_id);
    const pages = (S.version && S.version.manifest.pages) || [];
    if (!S.active || !pages.some((p) => p.id === S.active)) S.active = pages.length ? pages[0].id : null;
    if (S.version && S.version.spec && S.version.spec.sheets) {
      if (!S.sheet || !S.version.spec.sheets.some((s) => s.name === S.sheet)) S.sheet = S.version.spec.sheets[0].name;
    }
    draw();
    if (w.active_job && !S.job) attachJob(w.active_job.id);
    if (S.stopPrev) { S.stopPrev(); S.stopPrev = null; }
    const pv = S.version && S.version.previews;
    if (pv && pv.state === 'pending' && pv.job_id) {
      const vid = S.version.id;
      S.stopPrev = watchJob(pv.job_id, (j) => {
        if (['done', 'failed', 'cancelled'].includes(j.status) && S.version && S.version.id === vid) {
          S.stopPrev = null;
          if (j.status === 'done') load(S.viewingOld ? vid : undefined);
          else toast('历史版本预览生成失败：' + (j.error || ''), 'error');
        }
      });
    } else if (pv && pv.state === 'failed') {
      toast('历史版本预览生成失败：' + (pv.error || ''), 'error');
    }
  }

  const isImport = () => S.work.kind.startsWith('import_');
  const readonly = () => S.work.readonly || S.viewingOld;
  const man = () => (S.version ? S.version.manifest : {});

  // ---------- 顶栏 ----------
  function topBar() {
    const w = S.work, v = S.version;
    const title = h('div', { class: 'title', contenteditable: readonly() ? null : 'true', spellcheck: 'false',
      onkeydown: (e) => { if (e.key === 'Enter') { e.preventDefault(); e.target.blur(); } },
      onblur: async (e) => { const t = e.target.textContent.trim(); if (t && t !== w.title) { try { await patch(`/api/works/${w.id}`, { title: t }); w.title = t; toast('已重命名'); } catch (ex) { errToast(ex); } } } }, w.title);
    const ex = man().exports || {};
    const issues = unresolved();
    return h('div', { class: 'ed-top' },
      h('a', { href: '#/works', class: 'icon-btn', title: '返回作品库' }, '←'),
      title,
      h('button', { class: 'icon-btn', title: w.starred ? '取消星标' : '加星标', style: { color: w.starred ? '#f5a623' : null },
        onclick: async () => { await patch(`/api/works/${w.id}`, { starred: !w.starred }); w.starred = !w.starred; draw(); } }, w.starred ? '★' : '☆'),
      h('span', { class: 'badge' }, w.kind_label),
      v ? h('button', { class: 'btn small ghost', onclick: versionsPanel }, `第 ${v.number} 版 · ${SOURCE_LABELS[v.source] || v.source}`) : null,
      S.viewingOld ? [h('span', { class: 'badge warn' }, '正在查看历史版本'),
        !w.readonly ? h('button', { class: 'btn small', onclick: () => restore(v) }, '恢复此版本') : null,
        h('button', { class: 'btn small ghost', onclick: () => load() }, '返回最新')] : null,
      issues.length ? h('button', { class: 'btn small', style: { color: 'var(--danger)' }, onclick: issuesPanel }, `${issues.length} 处排版问题`) : null,
      h('span', { class: 'grow' }),
      Object.entries(ex).filter(([, e]) => e && e.file_id).map(([k, e]) =>
        h('button', { class: 'btn small' + (['pptx', 'docx', 'xlsx'].includes(k) ? ' primary' : ''), title: e.name, onclick: () => download(e.file_id) }, `下载 ${EXPORT_LABELS[k] || k}`)),
      ex.pdf && ex.pdf.file_id ? h('a', { class: 'btn small ghost', href: fileUrl(ex.pdf.file_id, true), target: '_blank', rel: 'noopener' }, '打印预览') : null,
      isImport() && !readonly() ? h('button', { class: 'btn small', onclick: rebuildDialog }, '重建为新作品') : null,
      w.readonly ? h('button', { class: 'btn small', onclick: async () => { const r = await post(`/api/works/${w.id}/copy`); location.hash = `#/work/${r.id}`; } }, '复制到我的工作区') : null,
      isOwner() ? h('button', { class: 'btn small ghost', onclick: shareDialog }, '分享') : null,
      !readonly() ? h('button', { class: 'icon-btn', title: '删除', onclick: async () => {
        if (await confirmDialog('删除作品', `把“${w.title}”移到回收站？`, '删除', true)) { await del(`/api/works/${w.id}`); location.hash = '#/works'; }
      } }, '🗑') : null);
  }

  function unresolved() {
    return (man().issues || []).filter((i) => !['fixed', 'visual_ok'].includes(i.status));
  }

  // ---------- 主体 ----------
  function draw() {
    if (!S.version) {
      clear(root, topBar(), h('div', { class: 'empty' }, '作品还在生成中……'), S.job ? h('div', { style: { padding: '0 20px' } }, S.jobEl) : null);
      return;
    }
    const kind = S.work.kind;
    let left, center;
    if (kind === 'ppt') [left, center] = pptViews();
    else if (kind === 'doc') [left, center] = docViews();
    else if (kind === 'xls') [left, center] = xlsViews();
    else [left, center] = importViews();
    clear(root, topBar(), h('div', { class: 'ed-body' }, left, center, rightPanel()));
  }

  // ----- PPT -----
  function pptViews() {
    const pages = man().pages || [];
    const changed = new Set(S.version.changed || []);
    const issueSlides = new Set(unresolved().map((i) => i.slide));
    const idx = Math.max(0, pages.findIndex((p) => p.id === S.active));
    const left = h('div', { class: 'ed-left thumbs' }, pages.map((p, i) => h('div', {
      class: 'th' + (p.id === S.active ? ' sel' : '') + (changed.has(p.id) ? ' changed' : '') + (issueSlides.has(p.id) ? ' issue' : ''),
      style: S.selSlides.has(p.id) ? { outline: '3px solid var(--primary)', outlineOffset: '1px' } : null,
      onclick: (e) => {
        if (e.ctrlKey || e.metaKey || e.shiftKey) { toggle(S.selSlides, p.id); S.selElems.clear(); }
        S.active = p.id; draw();
      } }, pageImg(p, `第 ${i + 1} 页`), h('span', { class: 'n' }, i + 1))));
    const cur = pages[idx];
    const size = man().size || { w: 13.333, h: 7.5 };
    const stage = h('div', { class: 'stage', style: { width: `min(100%, ${Math.round(900 * size.w / 13.333)}px)` } });
    if (cur) {
      stage.append(pageImg(cur, '', false));
      const issueEls = new Set(unresolved().filter((i) => i.slide === cur.id).map((i) => i.element));
      for (const e of ((man().elements || {})[cur.id] || [])) {
        if (e.decor || e.id.startsWith('_') || e.id.includes('._')) continue;
        const key = `${cur.id}|${e.id}`;
        const [x, y, w, hgt] = e.box;
        stage.append(h('div', {
          class: 'ovl' + (S.selElems.has(key) ? ' sel' : '') + (issueEls.has(e.id) ? ' issue' : ''),
          title: labelOf(e.id) + (readonly() ? '' : '（单击选中，双击直接编辑）'),
          style: { left: `${x * 100}%`, top: `${y * 100}%`, width: `${w * 100}%`, height: `${hgt * 100}%` },
          onclick: (ev) => { ev.stopPropagation(); if (!(ev.ctrlKey || ev.metaKey)) S.selElems.clear(); toggle(S.selElems, key); S.selSlides.clear(); draw(); },
          ondblclick: (ev) => { ev.stopPropagation(); if (!readonly()) editPptElement(cur.id, e); },
        }));
      }
    }
    const nav = h('div', { class: 'row gap' },
      h('button', { class: 'btn small', disabled: idx <= 0 || null, onclick: () => { S.active = pages[idx - 1].id; draw(); } }, '上一页'),
      h('span', { class: 'muted' }, `${idx + 1} / ${pages.length}`),
      h('button', { class: 'btn small', disabled: idx >= pages.length - 1 || null, onclick: () => { S.active = pages[idx + 1].id; draw(); } }, '下一页'),
      !readonly() && cur ? h('button', { class: 'btn small ghost', onclick: () => { toggle(S.selSlides, cur.id); S.selElems.clear(); draw(); } }, S.selSlides.has(cur.id) ? '取消选中本页' : '选中本页') : null);
    const slideSpec = cur && (S.version.spec.slides || []).find((s) => s.id === cur.id);
    const notes = slideSpec && slideSpec.notes ? h('div', { class: 'card', style: { width: 'min(100%, 900px)' } }, h('div', { class: 'muted small' }, '演讲备注'), h('div', {}, slideSpec.notes)) : null;
    const center = h('div', { class: 'ed-center', tabindex: 0, onclick: () => { if (S.selElems.size) { S.selElems.clear(); draw(); } },
      onkeydown: (e) => {
        if (e.key === 'ArrowDown' || e.key === 'PageDown') { if (idx < pages.length - 1) { S.active = pages[idx + 1].id; draw(); } }
        if (e.key === 'ArrowUp' || e.key === 'PageUp') { if (idx > 0) { S.active = pages[idx - 1].id; draw(); } }
      } }, nav, stage, notes, warningsBox());
    return [left, center];
  }

  function labelOf(path) {
    const map = { title: '标题', 'content.bullets': '要点', 'content.subtitle': '副标题', 'content.quote': '引用', 'content.table': '表格', 'content.chart': '图表', 'content.image': '图片' };
    if (map[path]) return map[path];
    const m = path.match(/content\.(\w+)\.(\d+)\.?(\w*)/);
    if (m) return `${m[1]} 第 ${Number(m[2]) + 1} 项 ${m[3]}`;
    return path;
  }

  function editPptElement(sid, e) {
    const slide = (S.version.spec.slides || []).find((s) => s.id === sid);
    if (!slide) return;
    const val = getPath(slide, e.id);
    let text, parse;
    if (typeof val === 'string') {
      text = val; parse = (t) => t;
    } else if (Array.isArray(val) && val.every((x) => typeof x === 'string')) {
      text = val.join('\n'); parse = (t) => t.split('\n').map((s) => s.trim()).filter(Boolean);
    } else if (Array.isArray(val) && val.every((x) => x && typeof x.text === 'string')) {
      text = val.map((b) => [b.text].concat((b.sub || []).map((s) => '  - ' + s)).join('\n')).join('\n');
      parse = (t) => {
        const out = [];
        for (const line of t.split('\n')) {
          if (!line.trim()) continue;
          if (/^\s+-\s*/.test(line) && out.length) out[out.length - 1].sub.push(line.replace(/^\s+-\s*/, '').trim());
          else out.push({ text: line.trim(), sub: [] });
        }
        return out;
      };
    } else if (val && Array.isArray(val.columns) && Array.isArray(val.rows)) {
      text = [val.columns].concat(val.rows).map((r) => r.map((c) => (c === null ? '' : String(c))).join('\t')).join('\n');
      parse = (t) => {
        const rows = t.split('\n').filter((l) => l.trim()).map((l) => l.split('\t').map(numOrStr));
        return { ...val, columns: rows[0].map(String), rows: rows.slice(1) };
      };
    } else if (val && Array.isArray(val.categories) && Array.isArray(val.series)) {
      text = [['分类'].concat(val.series.map((s) => s.name))].concat(val.categories.map((c, i) => [c].concat(val.series.map((s) => s.values[i])))).map((r) => r.join('\t')).join('\n');
      parse = (t) => {
        const rows = t.split('\n').filter((l) => l.trim()).map((l) => l.split('\t'));
        const names = rows[0].slice(1);
        return { ...val, categories: rows.slice(1).map((r) => r[0]), series: names.map((n, j) => ({ name: n, values: rows.slice(1).map((r) => Number(r[j + 1]) || 0) })) };
      };
    } else {
      toast('这个元素请用右侧对话修改');
      return;
    }
    const ta = h('textarea', { rows: Math.min(16, Math.max(3, text.split('\n').length + 1)), style: { fontFamily: 'inherit' } });
    ta.value = text;
    const hint = Array.isArray(val) && val.length && val[0].text !== undefined ? '每行一条要点；以两个空格加“- ”开头的行是上一条的子要点' : (val && val.columns ? '每行一行，单元格之间用 Tab 分隔，第一行是表头' : (val && val.categories ? '第一行是系列名，第一列是分类，用 Tab 分隔' : ''));
    modal(`直接编辑：${labelOf(e.id)}`, h('div', {}, ta, hint ? h('p', { class: 'muted small', style: { marginTop: '6px' } }, hint) : null), {
      actions: [{ label: '取消', kind: 'ghost', onClick: () => {} }, { label: '保存', kind: 'primary', onClick: async () => {
        let value;
        try { value = parse(ta.value); } catch (ex) { throw new Error('格式不正确'); }
        await runManual([{ op: 'set_field', slide_id: sid, path: e.id, value }]);
      } }],
    });
    setTimeout(() => ta.focus(), 50);
  }

  // ----- Word -----
  function docViews() {
    const blocks = (S.version.spec.blocks || []);
    const changed = new Set(S.version.changed || []);
    const bp = man().block_pages || {};
    const pages = man().pages || [];
    const pageEls = [];
    const center = h('div', { class: 'ed-center' }, warningsBox(), pages.map((p, i) => {
      const el = h('div', { class: 'docpage' }, pageImg(p), h('span', { class: 'pn' }, i + 1));
      pageEls.push(el);
      return el;
    }));
    const left = h('div', { class: 'ed-left outline' },
      h('div', { class: 'muted small', style: { margin: '4px 6px 8px' } }, readonly() ? '文档结构' : '单击选中（Ctrl 多选），双击直接编辑'),
      blocks.map((b) => {
        const label = b.text || (b.table && b.table.caption) || b.caption || b.latex || ({ toc: '目录', page_break: '分页', references: '参考文献', image: '图片', chart: '图表', table: '表格', list: '列表' }[b.type]) || b.type;
        const cls = b.type === 'heading' ? `l${b.level}` : 'blk';
        return h('div', { class: `it ${cls}` + (S.selBlocks.has(b.id) ? ' sel' : '') + (changed.has(b.id) ? ' changed' : ''), title: label,
          onclick: (e) => {
            if (!(e.ctrlKey || e.metaKey)) S.selBlocks.clear();
            toggle(S.selBlocks, b.id);
            const pg = bp[b.id];
            draw();
            if (pg) setTimeout(() => { const el = root.querySelectorAll('.docpage')[pg.page]; if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' }); }, 30);
          },
          ondblclick: () => { if (!readonly()) editDocBlock(b); } },
        b.type === 'heading' ? '' : '· ', label.slice(0, 40));
      }));
    return [left, center];
  }

  function editDocBlock(b) {
    let text, makeOp;
    if (['heading', 'paragraph', 'quote'].includes(b.type)) {
      text = b.text; makeOp = (t) => ({ op: 'set_text', block_id: b.id, text: t });
    } else if (b.type === 'list') {
      text = b.items.map((it) => [it.text].concat((it.sub || []).map((s) => '  - ' + s)).join('\n')).join('\n');
      makeOp = (t) => {
        const items = [];
        for (const line of t.split('\n')) {
          if (!line.trim()) continue;
          if (/^\s+-\s*/.test(line) && items.length) items[items.length - 1].sub.push(line.replace(/^\s+-\s*/, '').trim());
          else items.push({ text: line.trim(), sub: [] });
        }
        return { op: 'replace_block', block_id: b.id, block: { ...b, items } };
      };
    } else if (b.type === 'table') {
      text = [b.table.columns].concat(b.table.rows).map((r) => r.map((c) => (c === null ? '' : String(c))).join('\t')).join('\n');
      makeOp = (t) => {
        const rows = t.split('\n').filter((l) => l.trim()).map((l) => l.split('\t').map(numOrStr));
        return { op: 'replace_block', block_id: b.id, block: { ...b, table: { ...b.table, columns: rows[0].map(String), rows: rows.slice(1) } } };
      };
    } else if (b.type === 'equation') {
      text = b.latex; makeOp = (t) => ({ op: 'replace_block', block_id: b.id, block: { ...b, latex: t } });
    } else {
      toast('这个内容请用右侧对话修改');
      return;
    }
    const ta = h('textarea', { rows: Math.min(18, Math.max(4, text.split('\n').length + 2)) });
    ta.value = text;
    modal('直接编辑', h('div', {}, ta, h('p', { class: 'muted small', style: { marginTop: '6px' } },
      b.type === 'table' ? '单元格之间用 Tab 分隔，第一行是表头' : b.type === 'list' ? '每行一条；以两个空格加“- ”开头的行是子项' : b.type === 'equation' ? 'LaTeX 公式' : '可用 **粗体**、*斜体*、^上标^、~下标~')), {
      actions: [{ label: '取消', kind: 'ghost', onClick: () => {} }, { label: '保存', kind: 'primary', onClick: async () => { await runManual([makeOp(ta.value)]); } }],
    });
  }

  // ----- Excel -----
  function xlsViews() {
    const sheets = S.version.spec.sheets || [];
    const pv = (man().preview || {})[S.sheet] || { rows: [] };
    const sheetSpec = sheets.find((s) => s.name === S.sheet) || sheets[0];
    const issues = (man().issues || []).filter((i) => !i.sheet || i.sheet === S.sheet);
    const tabsEl = h('div', { class: 'tabs', style: { width: '100%' } }, sheets.map((s) => h('button', { class: 'tab' + (s.name === S.sheet ? ' active' : ''), onclick: () => { S.sheet = s.name; S.range = null; draw(); } }, s.name)));
    const ncol = Math.max(1, ...pv.rows.map((r) => r.length));
    const table = h('table', { class: 'sheet' });
    const letters = (n) => { let s = ''; n += 1; while (n > 0) { const m = (n - 1) % 26; s = String.fromCharCode(65 + m) + s; n = Math.floor((n - 1) / 26); } return s; };
    table.append(h('tr', {}, h('th', {}, ''), Array.from({ length: ncol }, (_, c) => h('th', {}, letters(c)))));
    const inRange = (r, c) => S.range && r >= S.range.r1 && r <= S.range.r2 && c >= S.range.c1 && c <= S.range.c2;
    pv.rows.forEach((row, r) => {
      const tr = h('tr', { class: r === 0 && sheetSpec && sheetSpec.columns.length ? 'hdr' : '' }, h('th', {}, r + 1));
      for (let c = 0; c < ncol; c++) {
        const v = row[c];
        const fmt = (pv.formats || [])[c] || '';
        const isNum = typeof v === 'number';
        const td = h('td', {
          class: (isNum ? 'num' : '') + (inRange(r, c) ? ' sel' : '') + (typeof v === 'string' && /^(#REF!|#NAME\?|#DIV\/0!|#VALUE!|#N\/A|Err:)/.test(v) ? ' err' : ''),
          title: cellFormula(sheetSpec, r, c) || '',
          onmousedown: (e) => { dragStart = [r, c]; S.range = { r1: r, c1: c, r2: r, c2: c }; paintSel(); e.preventDefault(); },
          onmouseenter: () => { if (dragStart) { S.range = { r1: Math.min(dragStart[0], r), c1: Math.min(dragStart[1], c), r2: Math.max(dragStart[0], r), c2: Math.max(dragStart[1], c) }; paintSel(); } },
          ondblclick: () => { if (!readonly()) editCell(sheetSpec, r, c, letters); },
        }, fmtCell(v, fmt, r === 0));
        td.dataset.rc = `${r},${c}`;
        tr.append(td);
      }
      table.append(tr);
    });
    const paintSel = () => {
      for (const td of table.querySelectorAll('td')) {
        const [r, c] = td.dataset.rc.split(',').map(Number);
        td.classList.toggle('sel', inRange(r, c));
      }
      drawScope();
    };
    const center = h('div', { class: 'ed-center', style: { alignItems: 'stretch' } }, tabsEl,
      issues.length ? h('div', { class: 'card', style: { borderColor: 'var(--danger)' } }, h('strong', { class: 'error' }, '验证发现的问题'),
        h('ul', { class: 'issues small' }, issues.map((i) => h('li', {}, i.message)))) : h('div', { class: 'muted small' }, '已用 LibreOffice 重算验证：没有错误值，公式引用都在数据区域内。双击单元格可直接编辑，拖动选择区域后可在右侧按区域修改。'),
      h('div', { class: 'sheet-wrap' }, table), pv.truncated ? h('div', { class: 'muted small' }, '只显示前 500 行、50 列') : null, warningsBox(),
      man().summary ? h('div', { class: 'card' }, h('strong', {}, '分析结论'), h('p', {}, man().summary)) : null);
    return [h('div', { class: 'ed-left', style: { width: '0', padding: '0', border: '0' } }), center];
  }

  function cellFormula(sheet, r, c) {
    if (!sheet) return '';
    const hdr = sheet.columns.length ? 1 : 0;
    if (r < hdr) return '';
    const row = sheet.rows[r - hdr];
    const v = row ? row[c] : null;
    return typeof v === 'string' && v.startsWith('=') ? v : '';
  }

  function editCell(sheet, r, c, letters) {
    const hdr = sheet.columns.length ? 1 : 0;
    let cur;
    if (r < hdr) cur = (sheet.columns[c] || {}).header || '';
    else { const row = sheet.rows[r - hdr] || []; cur = row[c] === null || row[c] === undefined ? '' : row[c]; }
    const inp = h('input', { type: 'text', value: String(cur) });
    modal(`编辑单元格 ${sheet.name}!${letters(c)}${r + 1}`, h('div', {}, inp, h('p', { class: 'muted small', style: { marginTop: '6px' } }, '以 = 开头表示公式；数字会按数字保存')), {
      actions: [{ label: '取消', kind: 'ghost', onClick: () => {} }, { label: '保存', kind: 'primary', onClick: async () => {
        await runManual([{ op: 'set_cell', sheet: sheet.name, cell: `${letters(c)}${r + 1}`, value: numOrStr(inp.value) }]);
      } }],
    });
    setTimeout(() => inp.focus(), 50);
  }

  // ----- 导入的文件 -----
  function importViews() {
    const pages = man().pages || [];
    const changed = new Set(S.version.changed || []);
    const isPptx = S.work.kind === 'import_pptx';
    const left = h('div', { class: 'ed-left thumbs' }, pages.map((p, i) => h('div', {
      class: 'th' + (changed.has(p.id) ? ' changed' : ''),
      style: S.selSlides.has(p.id) ? { outline: '3px solid var(--primary)', outlineOffset: '1px' } : null,
      onclick: () => {
        if (isPptx && !readonly()) { toggle(S.selSlides, p.id); drawScope(); draw(); }
        const el = root.querySelectorAll('.docpage')[i]; if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      } }, pageImg(p), h('span', { class: 'n' }, i + 1))));
    const rep = man().report || {};
    const center = h('div', { class: 'ed-center' },
      h('div', { class: 'card', style: { width: 'min(820px,100%)' } }, h('strong', {}, '导入报告'),
        h('div', { class: 'small', style: { marginTop: '6px' } }, h('div', {}, '可修改：', (rep.editable || []).join('、') || '无'),
          h('div', {}, '原样保留：', (rep.preserved || []).join('、') || '无'), (rep.notes || []).map((n) => h('div', { class: 'warn' }, n))),
        isPptx ? h('p', { class: 'muted small', style: { marginTop: '6px' } }, '在左侧点选页面，可以只修改选中的页面。') : null),
      warningsBox(),
      pages.map((p, i) => h('div', { class: 'docpage', style: isPptx ? { width: 'min(900px,100%)' } : null }, pageImg(p), h('span', { class: 'pn' }, i + 1))));
    return [left, center];
  }

  function warningsBox() {
    const w = [...(man().warnings || []), ...(man().fix_log || [])];
    if (man().coverage !== undefined) w.unshift(`原文文字覆盖率：${man().coverage}%`);
    if (!w.length) return null;
    return h('div', { class: 'card small', style: { width: 'min(900px,100%)' } }, h('strong', {}, '说明'), h('ul', { style: { margin: '6px 0 0', paddingLeft: '18px' } }, w.map((x) => h('li', {}, x))));
  }

  // ---------- 右侧：对话修改 ----------
  const scopeBox = h('div', { class: 'scope' });
  function scope() {
    const k = S.work.kind;
    if (k === 'ppt') {
      if (S.selElems.size) return { type: 'elements', ids: [...S.selElems] };
      if (S.selSlides.size) return { type: 'slides', ids: [...S.selSlides] };
    } else if (k === 'doc' && S.selBlocks.size) return { type: 'blocks', ids: [...S.selBlocks] };
    else if (k === 'xls' && S.range) {
      const L = (n) => { let s = ''; n += 1; while (n > 0) { const m = (n - 1) % 26; s = String.fromCharCode(65 + m) + s; n = Math.floor((n - 1) / 26); } return s; };
      return { type: 'range', range: `${S.sheet}!${L(S.range.c1)}${S.range.r1 + 1}:${L(S.range.c2)}${S.range.r2 + 1}` };
    } else if (k === 'import_pptx' && S.selSlides.size) {
      const pages = (man().pages || []).map((p) => p.id);
      return { type: 'slides', ids: [...S.selSlides].map((id) => pages.indexOf(id) + 1) };
    }
    return { type: 'all' };
  }
  function drawScope() {
    const sc = scope();
    const label = { all: '整份文档', slides: `选中的 ${(sc.ids || []).length} 页`, elements: `选中的 ${(sc.ids || []).length} 个元素`, blocks: `选中的 ${(sc.ids || []).length} 段内容`, range: `区域 ${sc.range}` }[sc.type];
    clear(scopeBox, h('span', { class: 'muted small' }, '修改范围：'), h('span', { class: 'chip' }, label),
      sc.type !== 'all' ? h('button', { class: 'icon-btn small', title: '清除选择', onclick: () => { S.selElems.clear(); S.selSlides.clear(); S.selBlocks.clear(); S.range = null; draw(); } }, '×') : null);
  }

  const chat = h('div', { class: 'chat' });
  const input = h('textarea', { rows: 3, placeholder: '输入修改指令，例如：第 3 页改成时间线；整份翻译成英文；语气更正式（Ctrl+Enter 发送）' });
  const track = h('input', { type: 'checkbox' });
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) send(); });
  const sendBtn = h('button', { class: 'btn primary', onclick: () => send() }, '发送');

  function rightPanel() {
    drawScope();
    drawChat();
    if (readonly()) {
      return h('div', { class: 'ed-right' }, h('div', { class: 'chat' }, h('p', { class: 'muted' }, S.work.readonly ? '这是管理员分享给你的只读作品，可以复制到自己的工作区后编辑。' : '正在查看历史版本。恢复此版本后才能继续修改。')));
    }
    return h('div', { class: 'ed-right' }, chat, h('div', { class: 'chat-input' }, scopeBox, input,
      h('div', { class: 'row between', style: { marginTop: '6px' } },
        S.work.kind === 'import_docx' ? h('label', { class: 'row gap small' }, track, '以修订模式写入') : h('span', { class: 'muted small' }, '只修改指定内容，其余保持不变'),
        sendBtn)));
  }

  function drawChat() {
    clear(chat, S.history.map((m) => h('div', { class: 'msg' + (m.me ? ' me' : '') }, m.text, m.meta ? h('div', { class: 'meta' }, m.meta) : null)),
      S.jobEl || null);
    setTimeout(() => { chat.scrollTop = chat.scrollHeight; }, 20);
  }

  async function loadHistory() {
    try {
      const vs = await api(`/api/works/${workId}/versions`);
      S.history = vs.items.slice().reverse().filter((v) => ['chat', 'inplace', 'manual', 'restore', 'generate', 'import'].includes(v.source)).map((v) =>
        ({ me: ['chat', 'inplace'].includes(v.source), text: v.source === 'generate' ? '生成了第 1 版' : v.source === 'import' ? '导入文件' : v.source === 'manual' ? '直接编辑' : v.source === 'restore' ? v.message : v.message,
          meta: `第 ${v.number} 版 · ${fmtTime(v.created_at)}` }));
    } catch (e) { S.history = []; }
  }

  async function send() {
    const text = input.value.trim();
    if (!text) return;
    if (S.job) return toast('上一条修改还在进行中');
    const sc = scope();
    const kind = isImport() ? 'import_edit' : 'edit';
    const params = { instruction: text, scope: sc, base_version_id: S.work.current_version_id };
    if (S.work.kind === 'import_docx') params.track_changes = track.checked;
    sendBtn.disabled = true;
    try {
      const job = await post('/api/jobs', { kind, work_id: workId, params });
      S.history.push({ me: true, text, meta: sc.type === 'all' ? '整份文档' : '限定范围' });
      input.value = '';
      attachJob(job.id, job);
    } catch (e) { errToast(e); } finally { sendBtn.disabled = false; }
  }

  async function runManual(ops) {
    if (S.job) throw new Error('有修改正在进行，请稍候');
    const job = await post('/api/jobs', { kind: 'manual_edit', work_id: workId, params: { ops, base_version_id: S.work.current_version_id } });
    attachJob(job.id, job);
  }

  async function attachJob(id, job) {
    job = job || await api(`/api/jobs/${id}`);
    S.job = job;
    S.jobEl = jobCard(job, { compact: true, onDone: async (j) => {
      S.job = null;
      S.jobEl = null;
      if (j.status === 'done') {
        const r = j.result || {};
        if (r.no_change) toast('没有需要修改的内容');
        S.selElems.clear(); S.selBlocks.clear(); S.range = null;
        await loadHistory();
        if (r.reply) S.history.push({ me: false, text: r.reply });
        await load();
        toast('已更新', 'ok');
      } else if (j.status === 'failed') {
        S.history.push({ me: false, text: '修改失败：' + (j.error || '') });
        draw();
      } else draw();
    } });
    S.stopJob = S.jobEl._stop;
    drawChat();
  }

  // ---------- 版本 ----------
  async function versionsPanel() {
    const vs = await api(`/api/works/${workId}/versions`);
    const list = h('table', { class: 't' }, h('tr', {}, h('th', {}, '版本'), h('th', {}, '来源'), h('th', {}, '说明'), h('th', {}, '时间'), h('th', {}, '')),
      vs.items.map((v) => h('tr', {},
        h('td', {}, `第 ${v.number} 版`, v.id === vs.current ? h('span', { class: 'badge done', style: { marginLeft: '6px' } }, '当前') : null),
        h('td', {}, SOURCE_LABELS[v.source] || v.source),
        h('td', { style: { maxWidth: '320px' } }, v.message || ''),
        h('td', { class: 'small muted' }, fmtDateTime(v.created_at)),
        h('td', { class: 'row gap' },
          h('button', { class: 'icon-btn', title: v.starred ? '取消星标（星标版本不会被自动清理）' : '加星标（不会被自动清理）', style: { color: v.starred ? '#f5a623' : null },
            onclick: async (e) => { await post(`/api/versions/${v.id}/star`, { starred: !v.starred }); v.starred = !v.starred; e.target.textContent = v.starred ? '★' : '☆'; } }, v.starred ? '★' : '☆'),
          h('button', { class: 'btn small', onclick: () => { m.close(); load(v.id); } }, '查看'),
          v.id !== vs.current ? h('button', { class: 'btn small', onclick: () => { m.close(); compare(v.id, vs.current); } }, '与当前对比') : null,
          v.id !== vs.current && !S.work.readonly ? h('button', { class: 'btn small', onclick: () => { m.close(); restore(v); } }, '恢复') : null))));
    const m = modal('版本历史', h('div', {}, h('p', { class: 'muted small' }, '每次生成和修改都会保存为一个版本。恢复旧版本会复制出一个新的最新版本，历史记录不会删除。'), list), { wide: true });
  }

  async function restore(v) {
    if (!(await confirmDialog('恢复版本', `把第 ${v.number} 版复制为新的最新版本？`, '恢复'))) return;
    try {
      await post(`/api/works/${workId}/restore`, { version_id: v.id });
      toast('已恢复', 'ok');
      await loadHistory();
      await load();
    } catch (e) { errToast(e); }
  }

  async function compare(a, b) {
    const d = await api(`/api/works/${workId}/diff?a=${a}&b=${b}`);
    if (d.previews_pending && d.previews_pending.length) {
      // 历史预览已清理：等重新生成完成后再打开对比
      toast('正在重新生成历史版本的预览，完成后自动打开对比');
      let left = d.previews_pending.length;
      d.previews_pending.forEach((jid) => watchJob(jid, (j) => {
        if (['done', 'failed', 'cancelled'].includes(j.status) && --left === 0) compare(a, b);
      }));
      return;
    }
    const label = { same: '未变', modified: '已修改', added: '新增', removed: '已删除' };
    const rows = d.pages.filter((p) => p.status !== 'same');
    const body = h('div', {},
      h('p', { class: 'muted small' }, `第 ${d.a.number} 版（左）与第 ${d.b.number} 版（右）：${rows.length} 页不同，${d.pages.length - rows.length} 页相同`),
      rows.map((p) => h('div', { class: 'diff-row' }, h('div', {}, h('span', { class: 'badge ' + (p.status === 'removed' ? 'failed' : p.status === 'added' ? 'done' : 'warn') }, label[p.status])),
        p.a ? h('img', { src: fileUrl(p.a, true), loading: 'lazy' }) : h('div', { class: 'muted' }, '（无）'),
        p.b ? h('img', { src: fileUrl(p.b, true), loading: 'lazy' }) : h('div', { class: 'muted' }, '（无）'))),
      d.text && d.text.length ? [h('h3', { style: { marginTop: '14px' } }, '文字变化'), d.text.map((t) => h('div', { class: 'card small' },
        t.removed ? h('del', {}, t.text) : t.ops.map((o) => o.op === 'equal' ? (o.a.length > 80 ? o.a.slice(0, 30) + ' … ' + o.a.slice(-30) : o.a)
          : o.op === 'delete' ? h('del', {}, o.a) : o.op === 'insert' ? h('ins', {}, o.b) : [h('del', {}, o.a), h('ins', {}, o.b)])))] : null);
    modal('版本对比', body, { wide: true });
  }

  function issuesPanel() {
    const pages = (man().pages || []).map((p) => p.id);
    const st = { unresolved: '仍有问题', needs_review: '需要人工确认' };
    modal('排版问题', h('div', {},
      h('p', { class: 'muted small' }, '系统已自动精简文字、缩小字号或拆页两轮，以下问题仍未解决。可以点击跳转到对应页面，用对话或直接编辑处理。'),
      h('ul', { class: 'issues' }, unresolved().map((i) => h('li', {},
        h('a', { href: '#', onclick: (e) => { e.preventDefault(); if (i.slide) { S.active = i.slide; S.selElems.clear(); if (i.element) S.selElems.add(`${i.slide}|${i.element}`); draw(); } } },
          i.slide ? `第 ${pages.indexOf(i.slide) + 1} 页` : '文档'), '：', i.message, i.status ? h('span', { class: 'badge warn', style: { marginLeft: '6px' } }, st[i.status] || i.status) : null,
        (i.vision || []).length ? h('div', { class: 'muted small' }, '视觉复查：' + i.vision.join('；')) : null)))), { wide: true });
  }

  async function shareDialog() {
    const ws = (await api('/api/admin/workspaces')).items.filter((w) => w.kind === 'guest');
    const sel = select(ws.map((w) => [w.id, `${w.name}（${w.works} 个作品）`]), '');
    const mode = select([['readonly', '只读分享（对方看到的是原件，不能修改）'], ['copy', '可编辑副本（复制一份到对方工作区）']], 'readonly');
    const cur = (S.work.shares || []).map((s) => h('div', { class: 'row between small' }, `${s.name}（只读）`,
      h('button', { class: 'btn small ghost', onclick: async () => { await del(`/api/works/${workId}/share/${s.workspace_id}`); toast('已取消分享'); } }, '取消分享')));
    modal('分享作品', h('div', {}, ws.length ? [field('分享给', sel), field('方式', mode)] : h('p', { class: 'muted' }, '还没有访客工作区。先在“临时令牌”中签发令牌。'),
      cur.length ? [h('h3', {}, '已分享'), cur] : null), {
      actions: ws.length ? [{ label: '取消', kind: 'ghost', onClick: () => {} }, { label: '分享', kind: 'primary', onClick: async () => {
        await post(`/api/works/${workId}/share`, { workspace_id: sel.value, mode: mode.value });
        toast('已分享', 'ok');
      } }] : [],
    });
  }

  function rebuildDialog() {
    const target = select([['ppt', '演示文稿（按本系统布局重新设计）'], ['doc', 'Word 文档（按本系统样式重新排版）']], S.work.kind === 'import_docx' ? 'doc' : 'ppt');
    const instr = h('textarea', { rows: 2, placeholder: '补充要求（可选），例如：换成科技风格、面向客户' });
    modal('重建为新作品', h('div', {}, h('p', { class: 'muted small' }, '提取文件中的文字、表格、图片和图表数据，用本系统的布局和主题重新生成一个新作品。原文件保持不变。'),
      field('目标', target), field('要求', instr)), {
      actions: [{ label: '取消', kind: 'ghost', onClick: () => {} }, { label: '开始重建', kind: 'primary', onClick: async () => {
        const job = await post('/api/jobs', { kind: 'rebuild', params: { work_id: workId, target: target.value, instruction: instr.value } });
        toast('已开始重建，完成后会出现在作品库中');
        location.hash = '#/jobs';
        return job;
      } }],
    });
  }

  await loadHistory();
  await load();
  return () => { if (S.stopJob) S.stopJob(); if (S.stopPrev) S.stopPrev(); document.removeEventListener('mouseup', onUp); };
}

// ---------- 辅助 ----------
function toggle(set, v) { if (set.has(v)) set.delete(v); else set.add(v); }

function getPath(obj, path) {
  let cur = obj;
  for (const p of path.split('.')) {
    if (cur === undefined || cur === null) return undefined;
    cur = Array.isArray(cur) ? cur[Number(p)] : cur[p];
  }
  return cur;
}

function numOrStr(s) {
  const t = String(s).trim();
  if (t === '') return null;
  if (/^[-+]?\d+(\.\d+)?$/.test(t)) return Number(t);
  return t;
}

function fmtCell(v, fmt, header) {
  if (v === null || v === undefined) return '';
  if (typeof v !== 'number' || header) return String(v);
  if (fmt.includes('%')) {
    const d = (fmt.split('.')[1] || '').replace(/[^0]/g, '').length;
    return (v * 100).toFixed(d) + '%';
  }
  if (fmt.includes('0.00')) return v.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (fmt.includes('#,##0')) return Math.round(v).toLocaleString('zh-CN');
  return Number.isInteger(v) ? String(v) : String(Math.round(v * 1e6) / 1e6);
}
