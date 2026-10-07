import { api, clear, confirmDialog, del, download, errToast, field, fmtDateTime, fmtNum, fmtSize, h, jobCard, modal, patch, post, put, select, state, toast, uploader } from '../lib.js';

const PAGES = { tokens: ['临时令牌', tokensPage], workspaces: ['工作区', workspacesPage], models: ['模型接口', modelsPage], settings: ['设置', settingsPage],
  usage: ['用量统计', usagePage], system: ['系统状态', systemPage], audit: ['审计日志', auditPage] };

export async function render(page, which) {
  const [title, fn] = PAGES[which];
  const body = h('div');
  clear(page, h('h1', {}, title), body);
  return fn(body);
}

// ---------- 令牌 ----------
async function tokensPage(box) {
  const labels = state.me.perm_labels;
  const note = h('input', { type: 'text', placeholder: '例如：张三-毕业答辩' });
  const amount = h('input', { type: 'number', min: 1, value: 3, style: { width: '90px' } });
  const unit = select([['1', '小时'], ['24', '天']], '24', { style: { width: '80px' } });
  const oneTime = h('input', { type: 'checkbox' });
  const perms = Object.entries(labels).map(([k, l]) => { const c = h('input', { type: 'checkbox', checked: true, value: k }); return [c, l]; });
  const q = {};
  for (const [k, l, ph] of [['gen_count', '生成次数', '不限'], ['tokens', '模型 token 上限', '不限'], ['upload_mb', '单文件上传（MB）', '默认'], ['storage_mb', '工作区存储（MB）', '不限'], ['concurrent', '同时运行任务数', '不限']]) {
    q[k] = [h('input', { type: 'number', min: 0, placeholder: ph }), l];
  }
  const wsSel = h('select');
  const list = h('div');
  const load = async () => {
    const [toks, wss] = await Promise.all([api('/api/admin/tokens'), api('/api/admin/workspaces')]);
    clear(wsSel, h('option', { value: '' }, '新建独立工作区'), wss.items.filter((w) => w.kind === 'guest').map((w) => h('option', { value: w.id }, `沿用：${w.name}（${w.works} 个作品）`)));
    const stateBadge = (t) => [
      h('span', { class: 'badge ' + (t.grant === 'active' ? 'active' : t.grant === 'revoked' ? 'revoked' : '') }, { active: '有效', revoked: '已撤销', expired: '已过期' }[t.grant]),
      ' ', h('span', { class: 'badge' }, t.redeem === 'redeemed' ? '已兑换' : '未兑换'), t.one_time ? h('span', { class: 'badge', style: { marginLeft: '4px' } }, '一次性') : null];
    clear(list, h('table', { class: 't' }, h('tr', {}, ['备注', '状态', '到期', '功能', '用量', '工作区', ''].map((x) => h('th', {}, x))),
      toks.items.map((t) => h('tr', {},
        h('td', {}, t.note), h('td', {}, stateBadge(t)), h('td', { class: 'small' }, fmtDateTime(t.expires_at)),
        h('td', { class: 'small' }, t.perms.map((p) => labels[p]).join('、')),
        h('td', { class: 'small' }, `生成 ${t.usage.gen}${t.quota.gen_count !== undefined ? '/' + t.quota.gen_count : ''} 次`, h('br'), `${fmtNum(t.usage.tokens)}${t.quota.tokens !== undefined ? '/' + fmtNum(t.quota.tokens) : ''} token`),
        h('td', { class: 'small' }, t.workspace, t.workspace_delete_after ? h('div', { class: 'warn' }, `将于 ${fmtDateTime(t.workspace_delete_after)} 删除`) : null),
        h('td', { class: 'row gap' },
          t.grant === 'active' ? [
            h('button', { class: 'btn small', onclick: () => extend(t) }, '延期'),
            h('button', { class: 'btn small danger', onclick: async () => { if (await confirmDialog('撤销令牌', `撤销“${t.note}”？已有会话会立即失效，正在运行的任务会被取消。`, '撤销', true)) { await post(`/api/admin/tokens/${t.id}/revoke`); load(); } } }, '撤销')]
            : h('button', { class: 'btn small', onclick: () => { wsSel.value = t.workspace_id; note.value = t.note; toast('已选择沿用该工作区，填写后签发即可续发'); } }, '续发'))))));
  };
  const extend = (t) => {
    const hours = h('input', { type: 'number', min: 1, value: 24 });
    modal('延期', field('从现在起再延长（小时）', hours), { actions: [{ label: '取消', kind: 'ghost', onClick: () => {} }, { label: '确定', kind: 'primary', onClick: async () => {
      await patch(`/api/admin/tokens/${t.id}`, { expires_at: Math.max(t.expires_at, Date.now() / 1000) + Number(hours.value) * 3600 }); load();
    } }] });
  };
  const create = async () => {
    const quota = {};
    for (const [k, [inp]] of Object.entries(q)) if (inp.value !== '') quota[k] = Number(inp.value);
    const body = { note: note.value.trim() || '访客', hours: Number(amount.value) * Number(unit.value), one_time: oneTime.checked,
      perms: perms.filter(([c]) => c.checked).map(([c]) => c.value), quota, workspace_id: wsSel.value || null };
    try {
      const r = await post('/api/admin/tokens', body);
      const copy = (text) => { navigator.clipboard.writeText(text).then(() => toast('已复制'), () => toast('复制失败，请手动选择复制', 'error')); };
      modal('令牌已签发', h('div', {},
        h('p', { class: 'warn' }, '令牌只显示这一次，请立即复制发给对方。'),
        field('访问链接（推荐，打开即进入）', h('div', { class: 'row gap' }, h('input', { type: 'text', value: r.link, readonly: true }), h('button', { class: 'btn small', onclick: () => copy(r.link) }, '复制'))),
        field('令牌', h('div', { class: 'row gap' }, h('input', { type: 'text', value: r.token, readonly: true, class: 'mono' }), h('button', { class: 'btn small', onclick: () => copy(r.token) }, '复制')))), { actions: [{ label: '完成', kind: 'primary', onClick: () => {} }] });
      note.value = '';
      load();
    } catch (e) { errToast(e); }
  };
  clear(box, h('div', { class: 'card' }, h('h2', {}, '签发令牌'),
    h('div', { class: 'grid c2' }, field('备注（发给谁、用途）', note), field('有效期', h('div', { class: 'row gap' }, amount, unit, h('label', { class: 'row gap small' }, oneTime, '一次性（只能兑换一次）')))),
    field('允许的功能', h('div', { class: 'checks' }, perms.map(([c, l]) => h('label', {}, c, l)))),
    h('div', { class: 'grid c3' }, Object.values(q).map(([inp, l]) => field(l, inp))),
    field('工作区', wsSel, '续发给同一个人时选择沿用原工作区，对方能继续看到之前的作品'),
    h('button', { class: 'btn primary', onclick: create }, '签发')),
  h('div', { class: 'card' }, h('h2', {}, '全部令牌'), list));
  await load();
}

// ---------- 工作区 ----------
async function workspacesPage(box) {
  const load = async () => {
    const r = await api('/api/admin/workspaces');
    clear(box, h('div', { class: 'card' }, h('p', { class: 'muted small' }, '每个令牌默认有独立工作区。令牌全部到期或撤销后，工作区保留一段时间（在“设置”中调整）再自动删除；续发令牌会取消删除计划。要查看访客的作品，在作品库中勾选“显示所有工作区”。'),
      h('table', { class: 't' }, h('tr', {}, ['名称', '类型', '作品', '存储', '令牌', '计划删除', ''].map((x) => h('th', {}, x))),
        r.items.map((w) => h('tr', {}, h('td', {}, w.name), h('td', {}, w.kind === 'owner' ? '管理员' : '访客'), h('td', {}, w.works), h('td', {}, fmtSize(w.storage_bytes)),
          h('td', {}, w.tokens), h('td', { class: 'small' }, w.delete_after ? fmtDateTime(w.delete_after) : ''),
          h('td', {}, w.kind === 'guest' ? h('button', { class: 'btn small danger', onclick: async () => {
            if (await confirmDialog('立即删除工作区', `删除“${w.name}”及其全部作品和文件？此操作不能撤销。`, '删除', true)) { await del(`/api/admin/workspaces/${w.id}`); load(); }
          } }, '立即删除') : null))))));
  };
  await load();
}

// ---------- 模型接口 ----------
async function modelsPage(box) {
  const load = async () => {
    const r = await api('/api/admin/endpoints');
    const eps = r.items;
    const models = r.models;
    const st = r.status;
    const yes = (ok, a, b) => h('span', { class: 'badge ' + (ok ? 'done' : ok === false ? 'failed' : '') }, ok ? a : b);
    const capBadges = (m) => {
      const c = m.capabilities || {};
      const out = [];
      if (c.ok === undefined && c.image === undefined) return h('span', { class: 'badge' }, '未测试');
      if (c.ok !== undefined) {
        out.push(yes(c.ok, '对话正常', '对话失败'));
        if (c.ok) {
          out.push(' ', yes(c.json, 'JSON 正常', 'JSON 不合格'), ' ', h('span', { class: 'badge ' + (c.vision ? 'done' : '') }, c.vision ? '能看图' : '不能看图'));
          if (c.json_mode === false) out.push(' ', h('span', { class: 'badge warn' }, '不支持 JSON 模式'));
          if (c.latency_ms) out.push(h('span', { class: 'muted small' }, ` ${c.latency_ms}ms`));
        }
      }
      if (c.image !== undefined) out.push(' ', yes(c.image, '能生图', '生图失败'));
      for (const k of ['error', 'image_error']) if (c[k]) out.push(h('div', { class: 'error small' }, c[k]));
      return out;
    };
    const usedBy = (id) => Object.entries(r.roles).filter(([, v]) => v === id).map(([k]) => r.role_labels[k] || '图库');
    const openaiEps = eps.filter((e) => e.kind === 'openai');

    // 角色分配：任何模型都可以指定给任何角色
    const modelSel = (k) => {
      const s = h('select', {}, h('option', { value: '' }, k === 'vision' ? '（未指定：规划模型能看图时由它兼任）' : '（未指定）'));
      for (const e of openaiEps) {
        const ms = models.filter((m) => m.endpoint_id === e.id);
        if (ms.length) s.append(h('optgroup', { label: e.name }, ms.map((m) => h('option', { value: m.id, selected: r.roles[k] === m.id ? true : null },
          m.name === m.model ? m.model : `${m.name}（${m.model}）`))));
      }
      return s;
    };
    const roles = { planner: modelSel('planner'), writer: modelSel('writer'), fast: modelSel('fast'), vision: modelSel('vision'), image: modelSel('image'),
      stock: select([['', '（未指定）']].concat(eps.filter((e) => e.kind === 'stock').map((e) => [e.id, e.name])), r.roles.stock || '') };
    // 选中模型的测试结果只作提示，不阻止保存
    const hint = (k, sel) => {
      const el = h('span', { class: 'hint' });
      const upd = () => {
        const m = models.find((x) => x.id === sel.value);
        const c = (m && m.capabilities) || {};
        el.textContent = !m ? '' : k === 'vision' && c.vision === false ? '测试显示该模型不能看图' : k === 'image' && c.image === false ? '测试显示该模型不能生图'
          : ['planner', 'writer', 'fast'].includes(k) && c.ok === false ? '测试显示该模型对话失败' : k === 'planner' && c.json === false ? '测试显示 JSON 输出不合格' : '';
      };
      sel.addEventListener('change', upd); upd();
      return el;
    };
    const roleField = (label, k) => h('label', { class: 'field' }, h('span', { class: 'label' }, label), roles[k], hint(k, roles[k]));

    clear(box,
      h('div', { class: 'card' }, h('div', { class: 'row between' }, h('h2', {}, '接口'), h('div', { class: 'row gap' },
        h('button', { class: 'btn primary', onclick: () => editEp(null, 'openai') }, '添加模型接口'), h('button', { class: 'btn', onclick: () => editEp(null, 'stock') }, '添加图库'))),
        h('p', { class: 'muted small' }, '模型接口使用 OpenAI 兼容协议（填写到 /v1 为止的地址）。添加后点“拉取模型”，从列表里勾选要用的模型；列表里没有的可以手动填写。'),
        eps.length ? h('table', { class: 't' }, h('tr', {}, ['名称', '类型', '地址', '模型', ''].map((x) => h('th', {}, x))),
          eps.map((e) => h('tr', {}, h('td', {}, e.name, e.enabled ? null : h('span', { class: 'badge', style: { marginLeft: '4px' } }, '停用'), e.has_key ? null : h('span', { class: 'badge warn', style: { marginLeft: '4px' } }, '无 Key')),
            h('td', {}, r.kinds[e.kind] || e.kind), h('td', { class: 'small' }, e.base_url),
            h('td', { class: 'small' }, e.kind === 'openai' ? [`已添加 ${models.filter((m) => m.endpoint_id === e.id).length} 个`,
              e.available_at ? h('span', { class: 'muted' }, ` · 接口提供 ${e.available.length} 个`) : null] : capBadges({ capabilities: e.capabilities })),
            h('td', { class: 'row gap' },
              e.kind === 'openai' ? [h('button', { class: 'btn small', onclick: () => fetchModels(e) }, '拉取模型'), h('button', { class: 'btn small ghost', onclick: () => pickModels(e) }, '添加模型')]
                : h('button', { class: 'btn small', onclick: () => runJob(`/api/admin/endpoints/${e.id}/test`, `测试：${e.name}`) }, '测试'),
              h('button', { class: 'btn small ghost', onclick: () => editEp(e, e.kind) }, '编辑'),
              h('button', { class: 'btn small ghost', onclick: async () => { if (await confirmDialog('删除接口', `删除“${e.name}”及其下的全部模型？`, '删除', true)) { await del(`/api/admin/endpoints/${e.id}`); load(); } } }, '删除'))))) : h('p', { class: 'muted' }, '还没有接口')),
      h('div', { class: 'card' }, h('h2', {}, '模型'),
        h('p', { class: 'muted small' }, '模型不分类型，“测试”只用来了解它能做什么（对话、JSON、看图、生图），由你在下面的角色分配里决定它负责哪些工作。同一个模型可以同时负责多个角色。'),
        models.length ? h('table', { class: 't' }, h('tr', {}, ['模型', '接口', '测试结果', '负责', '价格', ''].map((x) => h('th', {}, x))),
          models.map((m) => h('tr', {}, h('td', {}, m.name, m.name !== m.model ? h('div', { class: 'muted small' }, m.model) : null, m.enabled ? null : h('span', { class: 'badge', style: { marginLeft: '4px' } }, '停用')),
            h('td', { class: 'small' }, m.endpoint_name), h('td', {}, capBadges(m)),
            h('td', { class: 'small' }, usedBy(m.id).join('、') || h('span', { class: 'muted' }, '—')),
            h('td', { class: 'small' }, `输入 ${m.price_in} / 输出 ${m.price_out}`, m.price_image ? h('div', {}, `每张 ${m.price_image}`) : null),
            h('td', { class: 'row gap' },
              h('button', { class: 'btn small', onclick: () => runJob(`/api/admin/models/${m.id}/test`, `测试对话：${m.name}`, { what: 'chat' }) }, '测试对话'),
              h('button', { class: 'btn small ghost', onclick: () => runJob(`/api/admin/models/${m.id}/test`, `测试生图：${m.name}`, { what: 'image' }) }, '测试生图'),
              h('button', { class: 'btn small ghost', onclick: () => editModel(m) }, '编辑'),
              h('button', { class: 'btn small ghost', onclick: async () => { if (await confirmDialog('删除模型', `删除“${m.name}”？`, '删除', true)) { await del(`/api/admin/models/${m.id}`); load(); } } }, '删除'))))) : h('p', { class: 'muted' }, '还没有模型：先添加接口，再拉取模型')),
      h('div', { class: 'card' }, h('h2', {}, '角色分配'),
        h('div', { class: 'grid c3' }, roleField('规划（大纲、规格、修改操作）', 'planner'), roleField('写作（正文、备注、翻译）', 'writer'), roleField('快速（摘要、分类）', 'fast'),
          roleField('视觉（排版复查、看图）', 'vision'), roleField('图像生成（配图）', 'image'), roleField('图库（配图检索）', 'stock')),
        h('p', { class: 'muted small' }, '写作、快速未指定时依次使用规划、写作模型。'),
        h('button', { class: 'btn primary', onclick: async () => { const body = {}; for (const [k, s] of Object.entries(roles)) body[k] = s.value || null; await put('/api/admin/roles', body); toast('已保存', 'ok'); load(); } }, '保存角色'),
        h('div', { class: 'row gap wrap small', style: { marginTop: '10px' } }, [['text', '文字'], ['json', 'JSON 输出'], ['vision', '视觉复查'], ['image', '图像生成'], ['stock', '图库']].map(([k, l]) =>
          h('span', { class: 'badge ' + (st[k] ? 'done' : '') }, `${l}：${st[k] ? '可用' : '不可用'}`)))));
  };
  const runJob = async (url, title, body, after) => {
    const job = await post(url, body);
    const m = modal(title, jobCard(job, { onDone: (j) => { setTimeout(async () => { m.close(); await load(); if (after && j.status === 'done') after(j); }, 600); } }));
  };
  const fetchModels = (e) => runJob(`/api/admin/endpoints/${e.id}/fetch`, `拉取模型：${e.name}`, undefined, async () => {
    const r = await api('/api/admin/endpoints');
    const ep = r.items.find((x) => x.id === e.id);
    if (ep) pickModels(ep, r.models);
  });
  const pickModels = async (e, models) => {
    if (!models) models = (await api('/api/admin/endpoints')).models;
    const have = new Set(models.filter((m) => m.endpoint_id === e.id).map((m) => m.model));
    const avail = e.available || [];
    const q = h('input', { type: 'text', placeholder: `筛选（共 ${avail.length} 个）` });
    const boxes = avail.map((n) => { const c = h('input', { type: 'checkbox', value: n, checked: have.has(n) ? true : null, disabled: have.has(n) ? true : null });
      return [n, h('label', { class: 'row gap small', style: { padding: '2px 0' } }, c, n, have.has(n) ? h('span', { class: 'muted' }, '（已添加）') : null), c]; });
    const list = h('div', { style: { maxHeight: '50vh', overflow: 'auto', border: '1px solid var(--line, #ddd)', borderRadius: '6px', padding: '6px 10px' } },
      boxes.length ? boxes.map((b) => b[1]) : h('p', { class: 'muted small' }, e.available_at ? '接口没有返回模型' : '还没有拉取模型列表，可以先点“拉取模型”，或在下面手动填写'));
    q.addEventListener('input', () => { const v = q.value.trim().toLowerCase(); for (const [n, el] of boxes) el.hidden = !!v && !n.toLowerCase().includes(v); });
    const manual = h('input', { type: 'text', placeholder: '多个用逗号或空格分隔，例如 deepseek-chat, qwen-vl-max' });
    modal(`添加模型：${e.name}`, h('div', {}, avail.length ? field('筛选', q) : null, list, field('手动填写模型名', manual)), { wide: true, actions: [
      { label: '取消', kind: 'ghost', onClick: () => {} },
      { label: '添加', kind: 'primary', onClick: async () => {
        const names = boxes.filter((b) => b[2].checked && !b[2].disabled).map((b) => b[0]).concat(manual.value.split(/[\s,，]+/).filter(Boolean));
        if (!names.length) { toast('没有选择模型'); return false; }
        await post(`/api/admin/endpoints/${e.id}/models`, { models: names });
        toast(`已添加 ${names.length} 个模型，可以点“测试对话”了解能力，再到角色分配中指定`, 'ok');
        load();
      } }] });
  };
  const editEp = (e, kind) => {
    const name = h('input', { type: 'text', value: e ? e.name : '' });
    const base = h('input', { type: 'text', value: e ? e.base_url : '', placeholder: kind === 'stock' ? '留空使用官方地址' : 'https://api.example.com/v1' });
    const key = h('input', { type: 'password', placeholder: e && e.has_key ? '已保存（留空表示不修改）' : 'API Key' });
    const provider = select([['pexels', 'Pexels'], ['unsplash', 'Unsplash']], e && e.extra ? e.extra.provider || 'pexels' : 'pexels');
    const enabled = h('input', { type: 'checkbox', checked: e ? !!e.enabled : true });
    const body = h('div', {}, field('名称', name), kind === 'stock' ? field('图库服务', provider) : null, field('接口地址', base), field('API Key', key),
      h('label', { class: 'row gap' }, enabled, '启用'));
    modal(e ? '编辑接口' : (kind === 'stock' ? '添加图库' : '添加模型接口'), body, { actions: [{ label: '取消', kind: 'ghost', onClick: () => {} }, { label: '保存', kind: 'primary', onClick: async () => {
      const data = { kind, name: name.value, base_url: base.value, enabled: enabled.checked, extra: kind === 'stock' ? { provider: provider.value } : (e ? e.extra : {}) };
      if (key.value) data.api_key = key.value;
      if (e) { await patch(`/api/admin/endpoints/${e.id}`, data); toast('已保存', 'ok'); load(); return; }
      const r = await post('/api/admin/endpoints', data);
      await load();
      if (kind === 'openai') fetchModels({ id: r.id, name: data.name || '模型接口' });
      else toast('已保存，建议点“测试”', 'ok');
    } }] });
  };
  const editModel = (m) => {
    const name = h('input', { type: 'text', value: m.name });
    const pin = h('input', { type: 'number', step: '0.01', value: m.price_in });
    const pout = h('input', { type: 'number', step: '0.01', value: m.price_out });
    const pimg = h('input', { type: 'number', step: '0.001', value: m.price_image });
    const enabled = h('input', { type: 'checkbox', checked: !!m.enabled });
    modal(`编辑模型：${m.model}`, h('div', {}, field('显示名称', name), h('div', { class: 'grid c3' }, field('输入单价（每百万 token）', pin), field('输出单价（每百万 token）', pout), field('每张图片', pimg)),
      h('label', { class: 'row gap' }, enabled, '启用')), { actions: [{ label: '取消', kind: 'ghost', onClick: () => {} }, { label: '保存', kind: 'primary', onClick: async () => {
      await patch(`/api/admin/models/${m.id}`, { name: name.value || m.model, price_in: pin.value, price_out: pout.value, price_image: pimg.value, enabled: enabled.checked });
      toast('已保存', 'ok');
      load();
    } }] });
  };
  await load();
}

// ---------- 设置 ----------
async function settingsPage(box) {
  const r = await api('/api/admin/settings');
  const s = r.settings;
  const defs = [
    ['max_upload_mb', '单文件上传上限（MB）'], ['max_unzipped_mb', '解压后总大小上限（MB）'], ['max_pages', '单文件页数上限'], ['max_pixels', '单张图片像素上限'],
    ['tool_output_hours', '工具结果保留（小时）'], ['keep_versions', '每个作品保留的未加星标版本数'], ['trash_days', '回收站保留（天）'],
    ['workspace_retention_days', '令牌结束后工作区保留（天）'], ['preview_days', '旧版本预览图保留（天）'], ['disk_warn_percent', '磁盘告警阈值（%）'], ['disk_block_percent', '暂停上传与新任务阈值（%）'],
  ];
  const inputs = {};
  const fontsBox = h('div');
  const packBox = h('div');
  const loadFonts = async () => {
    const f = await api('/api/admin/fonts');
    clear(fontsBox, f.items.length ? h('table', { class: 't' }, f.items.map((x) => h('tr', {}, h('td', {}, x.name), h('td', {}, fmtSize(x.size)),
      h('td', {}, h('button', { class: 'btn small ghost', onclick: async () => { await del(`/api/admin/fonts/${encodeURIComponent(x.name)}`); loadFonts(); } }, '删除'))))) : h('p', { class: 'muted small' }, '没有上传字体'));
  };
  const fontUp = uploader({ multiple: false, accept: '.ttf,.otf,.ttc', label: '上传字体文件（仅限有合法授权的字体）', purpose: 'font', onChange: async () => {
    const rec = fontUp.records()[0];
    if (rec) { try { await post('/api/admin/fonts', { file_id: rec.id }); toast('字体已登记，之后的预览会使用它', 'ok'); loadFonts(); } catch (e) { errToast(e); } }
  } });
  clear(box,
    h('div', { class: 'card' }, h('h2', {}, '处理上限与保留时间'),
      h('div', { class: 'grid c3' }, defs.map(([k, l]) => { inputs[k] = h('input', { type: 'number', value: s[k] }); return field(l, inputs[k]); })),
      h('button', { class: 'btn primary', onclick: async () => { const body = {}; for (const [k, i] of Object.entries(inputs)) body[k] = Number(i.value); try { await put('/api/admin/settings', body); toast('已保存', 'ok'); } catch (e) { errToast(e); } } }, '保存'),
      h('p', { class: 'muted small', style: { marginTop: '8px' } }, `资源配置档：${r.profile}；访问地址：${r.public_url}（在 .env 中修改）`)),
    h('div', { class: 'card' }, h('h2', {}, '预览字体'), h('p', { class: 'muted small' }, '服务器预览默认用思源字体替代微软雅黑、宋体、仿宋等。上传有授权的字体文件后，预览会更接近在 Windows 上打开的效果。'), fontsBox, fontUp),
    h('div', { class: 'card' }, h('h2', {}, '兼容性测试包'), h('p', { class: 'muted small' }, '生成覆盖全部布局、图表类型、文档预设的样例文件，下载到电脑上用 Microsoft Office 和 WPS 逐一打开检查。'),
      h('button', { class: 'btn', onclick: async () => {
        const job = await post('/api/admin/compat_pack');
        clear(packBox, jobCard(job, { onDone: (j) => { if (j.status === 'done') for (const f of j.result.files) packBox.append(h('button', { class: 'btn small primary', onclick: () => download(f.file_id) }, `下载 ${f.name}`)); } }));
      } }, '生成测试包'), packBox),
    h('div', { class: 'card' }, h('h2', {}, '维护'), h('p', { class: 'muted small' }, '清理过期文件、临时目录并检查磁盘（这些任务也会定时自动运行）。'),
      h('button', { class: 'btn', onclick: async () => { const res = await post('/api/admin/maintenance', { tasks: ['expire_files', 'clean_tmp', 'disk_check', 'gc_blobs', 'prune_versions', 'purge_trash'] }); toast('完成', 'ok'); console.log(res); } }, '立即清理'),
      ' ', h('button', { class: 'btn', onclick: async () => { await post('/api/admin/maintenance', { tasks: ['backup_db'] }); toast('数据库已备份', 'ok'); } }, '立即备份数据库')));
  await loadFonts();
}

// ---------- 用量 ----------
async function usagePage(box) {
  const days = select([['7', '最近 7 天'], ['30', '最近 30 天'], ['90', '最近 90 天'], ['365', '最近一年']], '30');
  const body = h('div');
  const load = async () => {
    const r = await api(`/api/admin/usage?days=${days.value}`);
    const money = (x) => (x || 0).toFixed(4);
    const total = r.by_day.reduce((a, d) => ({ p: a.p + (d.prompt || 0), c: a.c + (d.completion || 0), cost: a.cost + (d.cost || 0) }), { p: 0, c: 0, cost: 0 });
    const maxDay = Math.max(1, ...r.by_day.map((d) => (d.prompt || 0) + (d.completion || 0)));
    const KIND = { gen_ppt: '生成 PPT', gen_doc: '生成 Word', gen_xls: '生成 Excel', edit: '对话修改', import_edit: '原位修改', rebuild: '重建', model_test: '接口测试' };
    clear(body,
      h('div', { class: 'grid c3' }, h('div', { class: 'card' }, h('div', { class: 'muted small' }, '输入 token'), h('div', { class: 'stat' }, fmtNum(total.p))),
        h('div', { class: 'card' }, h('div', { class: 'muted small' }, '输出 token'), h('div', { class: 'stat' }, fmtNum(total.c))),
        h('div', { class: 'card' }, h('div', { class: 'muted small' }, '估算费用（按后台填写的单价）'), h('div', { class: 'stat' }, money(total.cost)))),
      h('div', { class: 'card' }, h('h2', {}, '每日用量'), r.by_day.length ? h('div', {}, r.by_day.map((d) => h('div', { class: 'row gap small' },
        h('span', { style: { width: '90px' } }, d.day), h('div', { class: 'bar grow', style: { margin: '4px 0' } }, h('i', { style: { width: `${((d.prompt + d.completion) / maxDay) * 100}%` } })),
        h('span', { style: { width: '160px', textAlign: 'right' } }, `${fmtNum(d.prompt + d.completion)}（${money(d.cost)}）`)))) : h('p', { class: 'muted' }, '没有数据')),
      h('div', { class: 'card' }, h('h2', {}, '按令牌'), table(['令牌', '任务数', '输入', '输出', '图片', '费用'], r.by_token.map((t) => [t.note || t.token_id, t.jobs, fmtNum(t.prompt), fmtNum(t.completion), t.images || 0, money(t.cost)]))),
      h('div', { class: 'card' }, h('h2', {}, '按功能'), table(['功能', '任务数', 'token', '费用'], r.by_kind.map((k) => [KIND[k.kind] || k.kind, k.jobs, fmtNum(k.tokens), money(k.cost)]))),
      h('div', { class: 'card' }, h('h2', {}, '按模型'), table(['模型', '角色', '调用次数', '输入', '输出', '估算的调用', '费用'], r.by_model.map((m) => [m.model, m.role, m.calls, fmtNum(m.prompt), fmtNum(m.completion), m.estimated_calls, money(m.cost)]))));
  };
  days.addEventListener('change', load);
  clear(box, h('div', { class: 'row gap', style: { marginBottom: '12px' } }, days, h('span', { class: 'muted small' }, '接口不返回用量时按字数估算，已在“估算的调用”中标明')), body);
  await load();
}

function table(head, rows) {
  if (!rows.length) return h('p', { class: 'muted' }, '没有数据');
  return h('table', { class: 't' }, h('tr', {}, head.map((x) => h('th', {}, x))), rows.map((r) => h('tr', {}, r.map((c) => h('td', {}, c)))));
}

// ---------- 系统 ----------
async function systemPage(box) {
  const load = async () => {
    const s = await api('/api/admin/system');
    const QL = { ai: 'AI', render: '渲染', convert: '转换', ocr: 'OCR', preview: '预览' };
    clear(box,
      h('div', { class: 'grid c4' },
        h('div', { class: 'card' }, h('div', { class: 'muted small' }, `CPU（${s.cpu_count} 核）`), h('div', { class: 'stat' }, `${s.cpu_percent}%`), h('div', { class: 'muted small' }, `负载 ${s.load.map((x) => x.toFixed(2)).join(' / ')}`)),
        h('div', { class: 'card' }, h('div', { class: 'muted small' }, '内存'), h('div', { class: 'stat' }, `${s.memory.percent}%`), h('div', { class: 'muted small' }, `${fmtSize(s.memory.used)} / ${fmtSize(s.memory.total)}`)),
        h('div', { class: 'card' }, h('div', { class: 'muted small' }, '数据盘'), h('div', { class: 'stat ' + (s.disk.percent >= 90 ? 'error' : s.disk.percent >= 80 ? 'warn' : '') }, `${s.disk.percent}%`), h('div', { class: 'muted small' }, `${fmtSize(s.disk.used)} / ${fmtSize(s.disk.total)}`)),
        h('div', { class: 'card' }, h('div', { class: 'muted small' }, '24 小时内失败任务'), h('div', { class: 'stat ' + (s.failed_24h ? 'error' : '') }, s.failed_24h))),
      h('div', { class: 'card' }, h('h2', {}, '队列'), h('p', { class: 'muted small' }, `DocWork ${s.version || ''}；资源配置档 ${s.profile}：重负载任务全局并发 ${s.heavy_limit}，AI 任务并发 ${s.ai_limit}；OCR 引擎：${s.ocr_engine}；沙箱配置：${s.sandbox}`),
        sandboxNote(s.sandbox_effective),
        s.queues.length ? table(['队列', '状态', '数量'], s.queues.map((q) => [QL[q.queue] || q.queue, q.status, q.n])) : h('p', { class: 'muted' }, '队列空闲'),
        h('p', { class: 'muted small' }, '最近 5 分钟活动的 worker：' + (s.workers.map((w) => w.worker).join('，') || '无'))),
      h('div', { class: 'card' }, h('h2', {}, '最近失败的任务'), table(['任务', '错误', '时间'], s.recent_failed.map((j) => [j.title || j.kind, h('span', { class: 'small error' }, (j.error || '').slice(0, 200)), fmtDateTime(j.finished_at)]))),
      h('div', { class: 'card' }, h('h2', {}, '备份'), h('div', { class: 'kv small' },
        h('span', { class: 'muted' }, '数据库备份'), h('span', {}, s.last_backup ? fmtDateTime(s.last_backup.at) : '尚未备份'),
        h('span', { class: 'muted' }, 'restic 备份'), h('span', {}, s.last_restic ? `${fmtDateTime(s.last_restic.at)} ${s.last_restic.ok ? '成功' : '失败'}` : '未配置'))),
      h('button', { class: 'btn', onclick: load }, '刷新'));
  };
  await load();
}

// ---------- 审计 ----------
async function auditPage(box) {
  const r = await api('/api/admin/audit?limit=300');
  const A = { login: '登录', login_failed: '登录失败', redeem: '兑换令牌', token_created: '签发令牌', token_revoked: '撤销令牌', job_created: '创建任务', upload: '上传',
    download: '下载', work_trashed: '删除作品', work_deleted: '彻底删除作品', version_restored: '恢复版本', share_readonly: '只读分享', share_copy: '分享副本',
    workspace_deleted: '删除工作区', endpoint_saved: '保存接口', models_added: '添加模型', password_changed: '修改密码', totp_enabled: '开启两步验证', totp_disabled: '关闭两步验证' };
  clear(box, h('div', { class: 'card' }, table(['时间', '操作者', '操作', '对象', '详情', 'IP'], r.items.map((a) => [fmtDateTime(a.ts), a.actor, A[a.action] || a.action, h('span', { class: 'small mono' }, a.target), h('span', { class: 'small' }, JSON.stringify(a.detail)), a.ip]))));
}

// 各重负载 worker 实际生效的沙箱；降级为进程资源限制时明确提示
function sandboxNote(eff) {
  const rows = Object.entries(eff || {});
  if (!rows.length) return h('p', { class: 'muted small' }, '沙箱实际状态：重负载 worker 尚未报告（启动后显示）');
  const label = { bwrap: 'bubblewrap 隔离', rlimit: '仅进程资源限制（bubblewrap 不可用）', unavailable: '要求 bubblewrap 但不可用，任务会失败' };
  const weak = rows.some(([, v]) => v.mode !== 'bwrap');
  return h('p', { class: 'small', style: { color: weak ? 'var(--warn)' : null } },
    '沙箱实际状态：', rows.map(([q, v]) => `${q} → ${label[v.mode] || v.mode}`).join('；'),
    weak ? '。外部程序仍在无网络、只读根文件系统的容器中运行，但可以读写数据目录；详见 README“沙箱”一节。' : '');
}
