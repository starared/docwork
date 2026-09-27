import { clear, field, h, isOwner, post, state, toast, errToast } from '../lib.js';

export async function render(page) {
  const me = state.me;
  if (!isOwner()) {
    const q = me.quota || {}, u = me.usage || {};
    clear(page, h('h1', {}, '账户'), h('div', { class: 'card' }, h('div', { class: 'kv' },
      h('span', { class: 'muted' }, '身份'), h('span', {}, `访客：${me.note || ''}`),
      h('span', { class: 'muted' }, '有效期至'), h('span', {}, me.expires_at ? new Date(me.expires_at * 1000).toLocaleString('zh-CN') : ''),
      h('span', { class: 'muted' }, '可用功能'), h('span', {}, me.perms.map((p) => me.perm_labels[p] || p).join('、')),
      h('span', { class: 'muted' }, '已用生成次数'), h('span', {}, `${u.gen || 0}${q.gen_count !== undefined ? ' / ' + q.gen_count : ''}`),
      h('span', { class: 'muted' }, '已用模型用量'), h('span', {}, `${(u.tokens || 0).toLocaleString()}${q.tokens !== undefined ? ' / ' + Number(q.tokens).toLocaleString() : ''} token`),
      h('span', { class: 'muted' }, '存储'), h('span', {}, `${((u.storage_bytes || 0) / 1048576).toFixed(1)} MB${q.storage_mb !== undefined ? ' / ' + q.storage_mb + ' MB' : ''}`))),
      h('p', { class: 'muted small', style: { marginTop: '10px' } }, '令牌到期或被撤销后，工作区会保留一段时间再删除；需要长期保存的作品请及时下载。'));
    return;
  }
  const oldPw = h('input', { type: 'password', autocomplete: 'current-password' });
  const newPw = h('input', { type: 'password', autocomplete: 'new-password' });
  const totpBox = h('div');
  const drawTotp = () => {
    if (me.totp_enabled) {
      const code = h('input', { type: 'text', inputmode: 'numeric', placeholder: '当前验证码' });
      clear(totpBox, h('p', { class: 'ok' }, '两步验证已开启'), field('关闭两步验证', code),
        h('button', { class: 'btn', onclick: async () => { try { await post('/api/owner/totp/disable', { code: code.value }); me.totp_enabled = false; toast('已关闭'); drawTotp(); } catch (e) { errToast(e); } } }, '关闭'));
    } else {
      clear(totpBox, h('p', { class: 'muted' }, '开启后，登录时需要输入验证器 App（如 Google Authenticator、微软 Authenticator）中的 6 位验证码。'),
        h('button', { class: 'btn', onclick: async () => {
          const r = await post('/api/owner/totp/setup');
          const code = h('input', { type: 'text', inputmode: 'numeric', placeholder: '输入验证器中的 6 位数字' });
          clear(totpBox, h('p', {}, '在验证器 App 中手动添加账户，密钥为：'), h('p', { class: 'mono', style: { fontSize: '16px', letterSpacing: '1px' } }, r.secret),
            h('p', { class: 'muted small mono' }, r.uri), field('验证码', code),
            h('button', { class: 'btn primary', onclick: async () => { try { await post('/api/owner/totp/enable', { code: code.value }); me.totp_enabled = true; toast('两步验证已开启', 'ok'); drawTotp(); } catch (e) { errToast(e); } } }, '确认开启'));
        } }, '设置两步验证'));
    }
  };
  drawTotp();
  clear(page, h('h1', {}, '账户'),
    h('div', { class: 'card' }, h('h2', {}, '修改密码'), field('原密码', oldPw), field('新密码（至少 10 位）', newPw),
      h('button', { class: 'btn primary', onclick: async () => { try { await post('/api/owner/password', { old: oldPw.value, new: newPw.value }); toast('密码已修改，其他设备的登录已失效', 'ok'); oldPw.value = newPw.value = ''; } catch (e) { errToast(e); } } }, '保存')),
    h('div', { class: 'card' }, h('h2', {}, '两步验证'), totpBox));
}
