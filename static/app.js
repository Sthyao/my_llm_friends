const main = document.querySelector('#main');
const sidebar = document.querySelector('#session-sidebar');
const notice = document.querySelector('#notice');
const pages = ['chat', 'memory', 'ops'];
const collections = {
  providers: { name: 'API', item: 'API', empty: '添加 API 后，就可以为它配置模型。' },
  models: { name: '模型', item: '模型', empty: '添加聊天或生成总结所需的模型。' },
  bots: { name: '机器人', item: '机器人', empty: '将一个模型和一种人格绑定为机器人。' },
  personas: { name: '人格模板', item: '人格模板', empty: '定义一个机器人在讨论中的角色和回答习惯。' },
};
const state = {
  config: null, sessions: [], detail: null, route: null, loadId: 0, failed: '',
  search: '', sideOpen: !matchMedia('(max-width: 680px)').matches,
  selectedBots: null, drafts: new Map(), pending: new Map(), chatErrors: new Map(),
  summaryJobs: new Set(), noticeTimer: null, summaryModels: new Map(),
};

function esc(value) {
  return String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}

async function api(path, method = 'GET', body) {
  const options = { method };
  if (body !== undefined) {
    options.headers = { 'Content-Type': 'application/json' };
    options.body = JSON.stringify(body);
  }
  const response = await fetch('/api' + path, options);
  if (response.status === 204) return null;
  const text = await response.text();
  let data;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!response.ok) {
    const detail = data?.detail;
    const message = Array.isArray(detail) ? detail.map(item => item.msg).join('；') : detail;
    throw new Error(typeof message === 'string' ? message : '请求失败（' + response.status + '）');
  }
  return data;
}

function flash(message, isError = false) {
  clearTimeout(state.noticeTimer);
  notice.textContent = message;
  notice.classList.toggle('error', isError);
  notice.hidden = false;
  state.noticeTimer = setTimeout(() => { notice.hidden = true; }, isError ? 7000 : 3000);
}

function readRoute() {
  const [page, item, message] = location.hash.slice(1).split('/');
  if (page === 'ops') return { page, tab: Object.hasOwn(collections, item) ? item : 'providers' };
  const id = Number(item);
  const messageId = Number(message);
  return {
    page: pages.includes(page) ? page : 'chat',
    id: Number.isSafeInteger(id) && id > 0 ? id : null,
    message: Number.isSafeInteger(messageId) && messageId > 0 ? messageId : null,
    fresh: item === 'new',
  };
}

function href(page, id, message) {
  return '#' + page + (id ? '/' + id : '') + (message ? '/' + message : '');
}

function go(page, id, message) {
  const hash = href(page, id, message);
  if (location.hash === hash) return load();
  location.hash = hash;
}

function formatDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? '' : new Intl.DateTimeFormat('zh-CN', {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  }).format(date);
}

function enabledBots() {
  return state.config.bots.filter(bot => bot.enabled &&
    state.config.models.some(model => model.id === bot.model_id && model.enabled));
}

function selectedBots() {
  if (state.selectedBots === null) state.selectedBots = new Set(enabledBots().slice(0, 2).map(bot => bot.id));
  return enabledBots().filter(bot => state.selectedBots.has(bot.id)).map(bot => bot.id);
}

function sessionLinks() {
  const { page, id } = state.route;
  const query = state.search.trim().toLocaleLowerCase();
  const sessions = state.sessions.filter(session =>
    (page === 'memory' || !session.archived) &&
    (!query || session.title.toLocaleLowerCase().includes(query)));
  if (!sessions.length) return '<p class="side-note">' + (query ? '没有匹配的会话' : '还没有会话') + '</p>';
  return sessions.map(session => `<a class="session-link" href="${href(page, session.id)}"
    ${session.id === id ? 'aria-current="page"' : ''}><span>${esc(session.title)}</span>
    ${session.archived ? '<span>归档</span>' : session.has_summary ? '<span aria-label="有总结">·</span>' : ''}
    </a>`).join('');
}

function renderSidebar() {
  const page = state.route.page;
  document.querySelectorAll('[data-page]').forEach(link => {
    if (link.dataset.page === page) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
  if (page === 'ops') {
    sidebar.innerHTML = '';
    return;
  }
  sidebar.innerHTML = `<section class="session-section">
    <a class="side-button" href="#chat/new" data-action="new-chat">＋ 新对话</a>
    <details id="session-details" ${state.sideOpen ? 'open' : ''}>
      <summary class="session-section-heading">会话 ${state.sessions.length}</summary>
      <div class="session-filters">
        ${page === 'memory' ? `<input type="search" name="search" aria-label="搜索会话标题" placeholder="搜索会话" value="${esc(state.search)}">` : ''}
        <div class="session-list" id="session-list">${sessionLinks()}</div>
      </div>
    </details>
  </section>`;
  sidebar.querySelector('details').addEventListener('toggle', event => { state.sideOpen = event.currentTarget.open; });
}

function header(title, actions = '', label = '') {
  return `<header class="page-header"><div>${label ? '<p class="header-label">' + esc(label) + '</p>' : ''}
    <h1>${esc(title)}</h1></div>${actions ? '<div class="header-actions">' + actions + '</div>' : ''}</header>`;
}

function button(label, action, attributes = '', classes = '') {
  return `<button type="button" class="btn ${classes}" data-action="${action}" ${attributes}>${label}</button>`;
}

function referenceLink(reference, label) {
  if (!reference.session_id) return '';
  return `<a class="reference" href="${href('memory', Number(reference.session_id), Number(reference.message_id))}">${esc(label || reference.label || reference.session_title || '查看出处')}</a>`;
}

function renderMessage(message, highlighted, showId = false) {
  const user = message.role === 'user';
  const refs = (message.references || []).map(ref => {
    const prefix = ref.message_id ? '原话 #' + ref.message_id : ref.label;
    const label = [prefix, ref.session_title].filter(Boolean).join(' · ');
    return referenceLink(ref, label);
  }).join('');
  return `<article id="message-${message.id}" class="message${user ? ' message--user' : ''}${highlighted ? ' highlight' : ''}">
    <div class="message-meta"><strong>${esc(user ? '我' : message.bot_name || '机器人')}</strong>
    ${!user && Number(message.round) > 1 ? '<span class="tag">回应</span>' : ''}
    ${showId ? '<span class="message-number">#' + message.id + '</span>' : ''}
    <time datetime="${esc(message.created_at)}">${esc(formatDate(message.created_at))}</time></div>
    <div class="message-text">${esc(message.content)}</div>
    ${refs ? '<div class="references">' + refs + '</div>' : ''}</article>`;
}

function renderChat() {
  const detail = state.detail;
  const session = detail?.session;
  const draftKey = session?.id ?? 'new';
  const pending = state.pending.get(draftKey);
  const messages = detail?.messages ?? [];
  const chatErrors = state.chatErrors.get(draftKey) ?? [];
  const bots = enabledBots();
  const selected = selectedBots();
  const actions = session ? `<a class="btn" href="${href('memory', session.id)}">会话记忆</a>` : '';
  const thread = messages.length ? '<div class="thread">' + messages.map(msg => renderMessage(msg)).join('') + '</div>'
    : '<section class="chat-empty"><h2>你今天在想什么？</h2><p>记下一句，或者请机器人展开讨论。</p></section>';
  return `<div class="chat-page">${header(session?.title || '新对话', actions)}
    <div class="page-body chat-body" id="chat-body">${thread}
      ${pending ? '<p class="status-text" role="status">机器人正在回复…</p>' : ''}
      ${chatErrors.length ? '<div class="inline-error" role="alert">' + chatErrors.map(item =>
        '<p>' + esc([item.bot_name, item.detail].filter(Boolean).join(' · ')) + '</p>').join('') + '</div>' : ''}
    </div>
    ${session?.archived ? `<div class="composer-wrap"><div class="composer archive-notice">
      <span>该会话已归档。</span><a class="btn" href="${href('memory', session.id)}">到会话记忆恢复</a>
    </div></div>` : `<div class="composer-wrap"><form class="composer" data-form="chat">
      <textarea name="content" maxlength="12000" placeholder="写下一个想法…" aria-label="你的想法" required ${pending ? 'disabled' : ''}>${esc(state.drafts.get(draftKey) || '')}</textarea>
      <div class="composer-footer">
        <div class="bots" aria-label="选择机器人">${bots.length ? bots.map(bot =>
          `<label class="bot-option"><input type="checkbox" name="bot" value="${bot.id}" ${selected.includes(bot.id) ? 'checked' : ''}> ${esc(bot.name)}</label>`).join('')
          : '<a class="btn" href="#ops/bots">去配置机器人</a>'}</div>
        <div class="button-row">
          <button class="btn primary" type="submit" name="mode" value="send" ${pending || !selected.length ? 'disabled' : ''}>发送</button>
          <button class="btn" type="submit" name="mode" value="discuss" title="选择至少两个机器人" ${pending || selected.length < 2 ? 'disabled' : ''}>让他们讨论</button>
        </div>
      </div>
    </form></div>`}</div>`;
}

function summaryItems(items, sessionId) {
  return items?.length ? '<ul>' + items.map(item => {
    const refs = (item.source_ids || []).map(id => referenceLink({session_id: sessionId, message_id: id}, '原话 #' + id)).join('');
    return '<li>' + esc(item.text) + (refs ? '<div class="references">' + refs + '</div>' : '') + '</li>';
  }).join('') + '</ul>' : '<p class="muted small">暂无</p>';
}

function summaryCard(summary, sessionId) {
  if (!summary) return '<p class="muted small">生成后可以编辑，也可以设置是否供其他会话引用。</p>';
  return `<div class="summary-section">
    <div class="card-header"><h2>${esc(summary.title)}</h2>
    ${summary.stale ? '<span class="tag tag--pending">有新消息</span>' : ''}</div>
    <div class="tags">${summary.keywords.map(word => '<span class="tag">' + esc(word) + '</span>').join('')}</div>
    </div>
    <section class="summary-section"><h3>你的看法</h3>${summaryItems(summary.views, sessionId)}</section>
    <section class="summary-section"><h3>机器人观察</h3>${summaryItems(summary.observations, sessionId)}</section>
    <section class="summary-section"><h3>未聊完的问题</h3>${summary.questions.length ?
      '<ul>' + summary.questions.map(item => '<li>' + esc(item) + '</li>').join('') + '</ul>' :
      '<p class="muted small">暂无</p>'}</section>
    <p class="status-text">${esc(formatDate(summary.updated_at))}更新
      ${summary.through_message_id ? ' · 总结到 ' + referenceLink({session_id: sessionId, message_id: summary.through_message_id}, '消息 #' + summary.through_message_id) : ''}</p>
    <label class="checkbox-field memory-toggle"><input type="checkbox" name="share-summary" ${summary.share_enabled ? 'checked' : ''}> 供其他会话引用</label>
    <div class="button-row">${button('编辑总结', 'edit-summary')}${button('删除总结', 'delete-summary', '', 'danger')}</div>`;
}

function modelOptions(selected, models = state.config.models) {
  return models.map(model => `<option value="${model.id}" ${selected === model.id ? 'selected' : ''}>${esc(model.name)}</option>`).join('');
}

function renderMemory() {
  const detail = state.detail;
  if (!detail) return header('会话记忆') + `<div class="page-body">
    <section class="card empty-card"><h2>还没有打开会话</h2><p>选择一场会话查看原话和总结。</p><a href="#chat/new" class="btn primary">新对话</a></section></div>`;
  const { session, messages, summary } = detail;
  const pending = state.summaryJobs.has(session.id);
  const enabled = state.config.models.filter(model => model.enabled);
  const remembered = state.summaryModels.get(session.id) ?? state.config.summary_model_id ?? summary?.model_id;
  const chosen = enabled.find(model => model.id === remembered)?.id;
  const actions = `<a class="btn" href="${href('chat', session.id)}">打开对话</a>
    ${button('改名', 'rename-session')}${button(session.archived ? '取消归档' : '归档', 'archive-session')}
    ${button('删除', 'delete-session', '', 'danger')}`;
  return header(session.title, actions, '会话记忆') + `<div class="page-body memory-grid">
    <section><div class="card-header"><h2>会话原话</h2><span class="muted small">${messages.length} 条</span></div>
      <div class="thread">${messages.length ? messages.map(msg => renderMessage(msg, msg.id === state.route.message, true)).join('') :
      '<p class="muted">这场会话还没有消息。</p>'}</div></section>
    <section class="card"><div class="card-header"><h2>会话总结</h2></div>
      <form class="memory-controls" data-form="generate-summary">
        <label class="field"><span>总结模型</span><select name="model_id" ${!enabled.length || pending ? 'disabled' : ''}>
        ${enabled.length ? modelOptions(chosen, enabled) : '<option>请先启用模型</option>'}</select></label>
        <button type="submit" class="btn primary" ${!enabled.length || !messages.length || pending ? 'disabled' : ''}>${pending ? '正在生成…' : summary ? '重新生成' : '生成总结'}</button>
      </form>
      ${summaryCard(summary, session.id)}
    </section>
  </div>`;
}

function configName(collection, id) {
  return state.config[collection].find(row => row.id === id)?.name || '未知配置';
}

function configDescription(tab, item) {
  if (tab === 'providers') return [item.base_url, item.kind === 'deepseek' ? 'DeepSeek' : 'OpenAI 兼容', item.has_key ? '已配置密钥' : '未配置密钥'];
  if (tab === 'models') return [configName('providers', item.provider_id), item.model_code, item.enabled ? '已启用' : '已停用'];
  if (tab === 'bots') return [configName('models', item.model_id), configName('personas', item.persona_id), item.enabled ? '已启用' : '已停用'];
  return [item.prompt.length > 140 ? item.prompt.slice(0, 140) + '…' : item.prompt];
}

function renderOps() {
  const { tab } = state.route;
  const collection = collections[tab];
  const items = state.config[tab];
  const tabs = Object.entries(collections).map(([key, group]) => `<a href="#ops/${key}" ${key === tab ? 'aria-current="page"' : ''}>${group.name}</a>`).join('');
  const cards = items.map(item => `<article class="card ops-card">
    <div><h2>${esc(item.name)}</h2><p class="small muted wrap">${configDescription(tab, item).map(esc).join(' · ')}</p></div>
    <div class="button-row">
      ${tab === 'providers' ? button('测试连接', 'test-provider', 'data-id="' + item.id + '"') : ''}
      ${button('编辑', 'edit-config', 'data-id="' + item.id + '"')}
      ${tab === 'personas' ? button('复制', 'copy-config', 'data-id="' + item.id + '"') : ''}
      ${button('删除', 'delete-config', 'data-id="' + item.id + '"', 'danger')}
    </div>
  </article>`).join('');
  return header('运维', button('＋ 添加' + collection.item, 'add-config', '', 'primary')) +
    `<div class="page-body"><nav class="ops-tabs" aria-label="配置分类">${tabs}</nav>
    <div class="cards">${cards || '<section class="card empty-card"><h2>还没有' + collection.item + '</h2><p>' + collection.empty + '</p>' + button('添加' + collection.item, 'add-config', '', 'primary') + '</section>'}</div></div>`;
}

function render() {
  renderSidebar();
  if (state.failed) {
    main.innerHTML = header('想法簿') + '<div class="page-body"><div class="inline-error" role="alert">' +
      esc(state.failed) + '</div><p class="status-text">' + button('重新加载', 'reload') + '</p></div>';
  } else if (state.config) {
    main.innerHTML = state.route.page === 'ops' ? renderOps() : state.route.page === 'memory' ? renderMemory() : renderChat();
  }
  main.removeAttribute('aria-busy');
  main.inert = false;
}

async function load({ keepScroll = false } = {}) {
  const loadId = ++state.loadId;
  const route = readRoute();
  state.route = route;
  main.setAttribute('aria-busy', 'true');
  main.inert = true;
  try {
    const [config, sessions] = await Promise.all([api('/config'), api('/sessions')]);
    if (loadId !== state.loadId) return;
    if (!route.id && !route.fresh && route.page !== 'ops') {
      route.id = (route.page === 'chat' ? sessions.find(session => !session.archived) : sessions[0])?.id ?? null;
    }
    let detail;
    if (route.page !== 'ops' && route.id) detail = await api('/sessions/' + route.id);
    if (loadId !== state.loadId) return;
    state.config = config;
    state.sessions = sessions;
    state.detail = detail ?? null;
    state.failed = '';
    render();
    if (route.message) document.querySelector('#message-' + route.message)?.scrollIntoView({block: 'start'});
    else if (!keepScroll) {
      if (route.page === 'chat' && detail?.messages.length) window.scrollTo(0, document.body.scrollHeight);
      else window.scrollTo(0, 0);
    }
  } catch (error) {
    if (loadId !== state.loadId) return;
    state.failed = error.message;
    render();
  }
}

function formField(label, control) {
  return '<label class="field"><span>' + label + '</span>' + control + '</label>';
}

function textField(label, name, value, attributes = '') {
  return formField(label, `<input name="${name}" value="${esc(value)}" ${attributes}>`);
}

function textareaField(label, name, value, attributes = '') {
  return formField(label, `<textarea name="${name}" ${attributes}>${esc(value)}</textarea>`);
}

function selectField(label, name, items, selected) {
  return formField(label, `<select name="${name}" required>
    ${items.length ? items.map(item => `<option value="${esc(item.id)}" ${selected === item.id ? 'selected' : ''}>${esc(item.name)}</option>`).join('')
    : '<option value="">暂无配置</option>'}</select>`);
}

function checkboxField(label, name, checked) {
  return `<label class="checkbox-field"><input type="checkbox" name="${name}" ${checked ? 'checked' : ''}> ${label}</label>`;
}

function dialog(title, content, onSave) {
  const element = document.createElement('dialog');
  element.className = 'edit-dialog';
  element.innerHTML = `<h2>${esc(title)}</h2><form class="form-grid">
    ${content}<div class="inline-error" role="alert" hidden></div>
    <div class="button-row"><button class="btn primary" type="submit">保存</button><button class="btn" type="button" data-close>取消</button></div></form>`;
  document.body.append(element);
  const form = element.querySelector('form');
  const submit = form.querySelector('[type=submit]');
  const errorBox = form.querySelector('.inline-error');
  element.querySelector('[data-close]').addEventListener('click', () => element.close());
  element.addEventListener('close', () => element.remove(), { once: true });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const errorText = errorBox.textContent;
    submit.disabled = true;
    errorBox.hidden = true;
    try {
      await onSave(new FormData(form), form);
      element.close();
      flash('已保存');
    } catch (error) {
      errorBox.textContent = error.message || errorText || '保存失败';
      errorBox.hidden = false;
    } finally {
      submit.disabled = false;
    }
  });
  element.showModal();
  return element;
}

function showConfigEditor(tab, item, copy = false) {
  const editing = item && !copy;
  const name = copy ? item.name.slice(0, 115) + ' 副本' : item?.name;
  let fields = textField('名称', 'name', name, 'maxlength="120" required');
  if (tab === 'providers') {
    fields += textField('接口地址', 'base_url', item?.base_url, 'type="url" maxlength="1024" required placeholder="https://api.deepseek.com"');
    fields += selectField('协议', 'kind', [{id: 'openai', name: 'OpenAI Chat Completions 兼容'}, {id: 'deepseek', name: 'DeepSeek'}], item?.kind);
    fields += textField('API 密钥', 'api_key', '', 'type="password" autocomplete="off" maxlength="4096"' +
      (item?.has_key ? ' placeholder="留空则保留已配置密钥"' : ' placeholder="输入密钥"'));
  } else if (tab === 'models') {
    fields += selectField('所属 API', 'provider_id', state.config.providers, item?.provider_id);
    fields += textField('模型 ID', 'model_code', item?.model_code, 'maxlength="200" required placeholder="deepseek-flash"');
    fields += checkboxField('启用模型', 'enabled', item?.enabled ?? true);
  } else if (tab === 'bots') {
    fields += selectField('模型', 'model_id', state.config.models, item?.model_id);
    fields += selectField('人格模板', 'persona_id', state.config.personas, item?.persona_id);
    fields += checkboxField('启用机器人', 'enabled', item?.enabled ?? true);
  } else {
    fields += textareaField('角色设定', 'prompt', item?.prompt, 'rows="6" maxlength="12000" required');
  }

  dialog((editing ? '编辑' : copy ? '复制' : '添加') + collections[tab].item, fields, async data => {
    const body = Object.fromEntries(data);
    for (const key of ['provider_id', 'model_id', 'persona_id']) {
      if (Object.hasOwn(body, key)) body[key] = Number(body[key]);
    }
    if (tab === 'bots' || tab === 'models') body.enabled = data.has('enabled');
    if (tab === 'providers' && !body.api_key.trim()) delete body.api_key;
    await api('/config/' + tab + (editing ? '/' + item.id : ''), editing ? 'PATCH' : 'POST', body);
    await load({ keepScroll: true });
  });
}

function summaryEditRow(key, item) {
  const label = key === 'views' ? '看法' : '观察';
  return `<div class="summary-edit-row">
    <textarea name="${key}-text" rows="2" maxlength="1000" aria-label="${label}内容" placeholder="${label}内容">${esc(item?.text)}</textarea>
    <button class="btn text" type="button" data-summary-row="remove" aria-label="移除${label}">移除</button>
    <input name="${key}-sources" aria-label="${label}来源消息编号" placeholder="来源消息编号，以逗号分隔" value="${esc(item?.source_ids.join(', '))}">
  </div>`;
}

function summaryEditSection(key, label, items) {
  return `<section class="summary-editor-section"><h3>${label}</h3>
    <div data-summary-list="${key}">${items.map(item => summaryEditRow(key, item)).join('')}</div>
    <button class="btn" type="button" data-summary-row="${key}">＋ 添加${label}</button></section>`;
}

function parseList(value, separators) {
  return value.split(separators).map(item => item.trim()).filter(Boolean);
}

function summaryEdits(form, key) {
  return Array.from(form.querySelectorAll(`[data-summary-list="${key}"] .summary-edit-row`)).map(row => {
    const text = row.querySelector(`[name="${key}-text"]`).value.trim();
    const sources = row.querySelector(`[name="${key}-sources"]`).value;
    const sourceIds = parseList(sources, /[,，\s]+/).map(Number);
    if (sourceIds.some(id => !Number.isSafeInteger(id) || id < 1)) throw new Error('来源消息编号必须是正整数。');
    return { text, source_ids: Array.from(new Set(sourceIds)) };
  }).filter(item => item.text);
}

function editSummary(sessionId, summary) {
  const fields = textField('标题', 'title', summary.title, 'maxlength="120" required') +
    textField('关键词（以逗号分隔）', 'keywords', summary.keywords.join('，')) +
    summaryEditSection('views', '看法', summary.views) +
    summaryEditSection('observations', '观察', summary.observations) +
    textareaField('未聊完的问题（每行一条）', 'questions', summary.questions.join('\n'), 'rows="3"');
  const modal = dialog('编辑总结', fields, async (data, form) => {
    await api('/sessions/' + sessionId + '/summary', 'PATCH', {
      title: data.get('title'),
      keywords: parseList(data.get('keywords'), /[,，]/),
      views: summaryEdits(form, 'views'),
      observations: summaryEdits(form, 'observations'),
      questions: parseList(data.get('questions'), /\n/),
    });
    await load({ keepScroll: true });
  });
  modal.addEventListener('click', event => {
    const key = event.target.closest('[data-summary-row]')?.dataset.summaryRow;
    if (key === 'remove') event.target.closest('.summary-edit-row').remove();
    else if (key === 'views' || key === 'observations') {
      const list = modal.querySelector(`[data-summary-list="${key}"]`);
      list.insertAdjacentHTML('beforeend', summaryEditRow(key));
      list.lastElementChild.querySelector('textarea').focus();
    }
  });
}

function isViewing(sessionId, page) {
  return state.route?.id === sessionId && (!page || state.route.page === page);
}

async function pollChat(sessionId) {
  if (!isViewing(sessionId, 'chat')) return;
  try {
    const detail = await api('/sessions/' + sessionId);
    if (!state.pending.has(sessionId) || !isViewing(sessionId, 'chat')) return;
    if (detail.messages.length > (state.detail?.messages.length ?? 0)) {
      const nearBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 150;
      state.detail = detail;
      main.innerHTML = renderChat();
      if (nearBottom) window.scrollTo(0, document.body.scrollHeight);
    }
  } catch {
    // The original request owns the error shown to the user.
  }
}

async function sendChat(form, discuss) {
  const content = form.elements.content.value.trim();
  const botIds = selectedBots();
  const session = state.detail?.session;
  let sessionId = session?.id;
  let draftKey = sessionId ?? 'new';
  if (!content || botIds.length < (discuss ? 2 : 1) || state.pending.has(draftKey)) return;
  if (botIds.length > 6) return flash('每轮最多选择六个机器人。', true);
  const previousHash = location.hash;
  state.pending.set(draftKey, true);
  state.chatErrors.delete(draftKey);
  render();

  let poll;
  try {
    if (!sessionId) {
      const created = await api('/sessions', 'POST', {});
      sessionId = created.id;
      state.drafts.delete('new');
      state.pending.delete('new');
      draftKey = sessionId;
      state.pending.set(draftKey, true);
      if (location.hash === previousHash) go('chat', sessionId);
    }
    state.drafts.delete(draftKey);
    poll = setInterval(() => { void pollChat(sessionId); }, 1500);
    const response = await api('/sessions/' + sessionId + '/chat', 'POST', { content, bot_ids: botIds, discuss });
    state.chatErrors.set(draftKey, response.errors || []);
  } catch (error) {
    state.drafts.set(draftKey, content);
    state.chatErrors.set(draftKey, [{ detail: error.message }]);
    flash(error.message, true);
  } finally {
    clearInterval(poll);
    state.pending.delete(draftKey);
    if (isViewing(sessionId, 'chat') || (!sessionId && location.hash === previousHash)) await load();
  }
}

async function generateSummary(form) {
  const id = state.detail.session.id;
  if (state.summaryJobs.has(id)) return;
  const modelId = Number(new FormData(form).get('model_id'));
  if (modelId) state.summaryModels.set(id, modelId);
  state.summaryJobs.add(id);
  render();
  try {
    await api('/sessions/' + id + '/summary', 'POST', modelId ? { model_id: modelId } : {});
    flash('总结已更新');
  } catch (error) {
    flash(error.message, true);
  } finally {
    state.summaryJobs.delete(id);
    if (isViewing(id, 'memory')) await load({ keepScroll: true });
  }
}

async function dispatchAction(control) {
  const action = control.dataset.action;
  const session = state.detail?.session;
  const path = session ? '/sessions/' + session.id : null;
  const tab = state.route.tab;
  const item = tab ? state.config[tab].find(item => item.id === Number(control.dataset.id)) : null;
  if (action === 'new-chat') {
    if (location.hash === '#chat/new') main.querySelector('textarea')?.focus();
  } else if (action === 'reload') {
    await load();
  } else if (action === 'add-config' || action === 'edit-config' || action === 'copy-config') {
    showConfigEditor(tab, item, action === 'copy-config');
  } else if (action === 'delete-config') {
    if (!confirm('删除「' + item.name + '」？')) return;
    await api('/config/' + tab + '/' + item.id, 'DELETE');
    await load({ keepScroll: true });
    flash('配置已删除');
  } else if (action === 'test-provider') {
    control.disabled = true;
    control.textContent = '测试中…';
    try {
      const result = await api('/config/providers/' + item.id + '/test', 'POST', {});
      flash(result.message);
    } finally {
      control.disabled = false;
      control.textContent = '测试连接';
    }
  } else if (action === 'rename-session') {
    dialog('重命名会话', textField('标题', 'title', session.title, 'maxlength="120" required'), async data => {
      await api(path, 'PATCH', { title: data.get('title') });
      await load({ keepScroll: true });
    });
  } else if (action === 'archive-session') {
    await api(path, 'PATCH', { archived: !session.archived });
    await load({ keepScroll: true });
    flash(session.archived ? '已恢复会话' : '已归档会话');
  } else if (action === 'delete-session') {
    if (!confirm('删除「' + session.title + '」及其中的消息和总结？')) return;
    await api(path, 'DELETE');
    await go('memory');
    flash('会话已删除');
  } else if (action === 'edit-summary') {
    editSummary(session.id, state.detail.summary);
  } else if (action === 'delete-summary') {
    if (!confirm('删除这场会话的总结？')) return;
    await api(path + '/summary', 'DELETE');
    await load({ keepScroll: true });
    flash('总结已删除');
  }
}

document.addEventListener('click', event => {
  const control = event.target.closest('[data-action]');
  if (control && (state.config || control.dataset.action === 'reload')) {
    dispatchAction(control).catch(error => flash(error.message, true));
  }
});

document.addEventListener('submit', event => {
  const form = event.target;
  if (form.dataset.form === 'chat') {
    event.preventDefault();
    void sendChat(form, event.submitter?.value === 'discuss');
  } else if (form.dataset.form === 'generate-summary') {
    event.preventDefault();
    void generateSummary(form);
  }
});

document.addEventListener('input', event => {
  const input = event.target;
  if (input.name === 'search') {
    state.search = input.value;
    sidebar.querySelector('#session-list').innerHTML = sessionLinks();
  } else if (input.name === 'content') {
    state.drafts.set(state.detail?.session.id ?? 'new', input.value);
  }
});

document.addEventListener('change', async event => {
  const input = event.target;
  if (input.name === 'bot') {
    if (input.checked) state.selectedBots.add(Number(input.value));
    else state.selectedBots.delete(Number(input.value));
    const count = selectedBots().length;
    const form = input.closest('form');
    const pending = state.pending.has(state.detail?.session.id ?? 'new');
    form.querySelector('[value="send"]').disabled = pending || !count;
    form.querySelector('[value="discuss"]').disabled = pending || count < 2;
  } else if (input.name === 'model_id' && input.closest('[data-form="generate-summary"]')) {
    state.summaryModels.set(state.detail.session.id, Number(input.value));
  } else if (input.name === 'share-summary') {
    const id = state.detail.session.id;
    const checked = input.checked;
    input.disabled = true;
    try {
      const summary = await api('/sessions/' + id + '/summary', 'PATCH', { share_enabled: checked });
      if (isViewing(id, 'memory')) state.detail.summary = summary;
      flash(checked ? '总结会参与跨会话引用' : '已停止跨会话引用');
    } catch (error) {
      input.checked = !checked;
      flash(error.message, true);
    } finally {
      input.disabled = false;
    }
  }
});

document.addEventListener('keydown', event => {
  if (event.target.name === 'content' && event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
    const submit = event.target.form.querySelector('[value="send"]');
    if (submit.disabled) return;
    event.preventDefault();
    event.target.form.requestSubmit(submit);
  }
});

window.addEventListener('hashchange', () => {
  state.search = '';
  void load();
});
void load();
