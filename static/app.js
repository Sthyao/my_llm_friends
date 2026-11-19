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
const monitorTabs = { monitor: '使用监控', web: '网页搜索', alerts: 'API 告警' };
const speakerVoices = new Map([
  ['造梦师', 'dream'],
  ['刺头', 'critic'],
  ['拾荒者', 'keeper'],
  ['影子自我', 'mirror'],
]);
const state = {
  config: null, sessions: [], detail: null, route: null, loadId: 0, failed: '',
  search: '', sideOpen: !matchMedia('(max-width: 680px)').matches,
  drafts: new Map(), pending: new Map(), chatErrors: new Map(),
  summaryJobs: new Set(), rewindEditors: new Map(), noticeTimer: null, summaryModels: new Map(),
  evolution: null, sideTargetId: null, sideJobs: new Set(), openedAsides: new Set(), openedCalls: new Set(), pollTimer: null,
  webSearch: { busy: false, query: '', botId: '', result: null, error: '' },
  monitor: null, monitorFilters: { period: '24h', provider_id: '', model_id: '' },
  modelTests: new Map(),
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
    const error = new Error(typeof message === 'string' ? message : '请求失败（' + response.status + '）');
    error.status = response.status;
    throw error;
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
  const id = Number(item);
  const messageId = Number(message);
  if (page === 'ops') return {
    page,
    tab: item === 'backup' || Object.hasOwn(collections, item) || Object.hasOwn(monitorTabs, item) ? item : 'providers',
    botId: item === 'bots' && Number.isSafeInteger(messageId) && messageId > 0 ? messageId : null,
  };
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
    <a class="side-button" href="#chat/new" data-action="new-chat"><span class="new-chat-mark" aria-hidden="true">＋</span>新对话</a>
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

function renderRewindEditor(editor, busy) {
  const messageId = Number(editor.messageId);
  return `<form class="rewind-editor" data-form="rewind-chat" data-message-id="${messageId}">
    <textarea name="edited_content" aria-label="编辑这条消息" aria-describedby="rewind-note-${messageId}"
      maxlength="20000" required ${busy ? 'disabled' : ''}>${esc(editor.content)}</textarea>
    <div class="rewind-editor-footer">
      <p id="rewind-note-${messageId}" class="small muted">保存后，后续消息会撤回，群友会从这里重新接话。</p>
      <div class="button-row">
        ${button('取消', 'cancel-rewind', busy ? 'disabled' : '')}
        <button class="btn primary" type="submit" ${busy ? 'disabled' : ''}>保存并重跑</button>
      </div>
    </div>
  </form>`;
}

function renderMessage(message, { highlighted, showId, replyTarget, page, canRewind, editor, busy }) {
  const user = message.role === 'user';
  const speaker = user ? '我' : message.bot_name || '机器人';
  const voice = user ? 'user' : speakerVoices.get(speaker) ?? 'default';
  const refs = (message.references || []).map(ref => {
    const prefix = ref.message_id ? '原话 #' + ref.message_id : ref.label;
    const label = [prefix, ref.session_title].filter(Boolean).join(' · ');
    return referenceLink(ref, label);
  }).join('');
  const peerReply = !user && replyTarget && (replyTarget.role === 'assistant' || message.channel === 'aside')
    ? `<a class="reply-target" href="${href(page, Number(message.session_id || state.route.id), Number(replyTarget.id))}">接 ${esc(replyTarget.bot_name || '我')} 的话</a>`
    : '';
  const rewind = user && canRewind
    ? `<button type="button" class="message-rewind" data-action="rewind-chat" data-message-id="${Number(message.id)}">回退到这条</button>`
    : '';
  const thought = !user && message.inner_thought
    ? `<details class="character-aside" data-aside-key="${Number(message.id)}" ${state.openedAsides.has(message.id) ? 'open' : ''}>
        <summary>角色旁白</summary><p>${esc(message.inner_thought)}</p></details>`
    : '';
  const attribution = !user && (message.provider_name || message.model_name || message.model_code)
    ? `<details class="message-call" data-call-key="${Number(message.id)}" ${state.openedCalls.has(Number(message.id)) ? 'open' : ''}>
        <summary>调用信息</summary><dl>
          ${message.provider_name ? '<dt>API</dt><dd>' + esc(message.provider_name) + '</dd>' : ''}
          ${message.model_name || message.model_code ? '<dt>模型</dt><dd>' +
            esc(message.model_name || message.model_code) + (message.model_name && message.model_code
              && message.model_name !== message.model_code ? ' · ' + esc(message.model_code) : '') + '</dd>' : ''}
        </dl></details>` : '';
  return `<article id="message-${message.id}" class="message${user ? ' message--user' : ''}${highlighted ? ' highlight' : ''}" data-voice="${voice}">
    <div class="message-meta"><span class="speaker-mark" aria-hidden="true">${esc(Array.from(speaker)[0])}</span><strong>${esc(speaker)}</strong>
    ${peerReply}
    ${showId && message.channel === 'aside' ? '<span class="tag">交头接耳</span>' : ''}
    ${showId ? '<span class="message-number">#' + message.id + '</span>' : ''}
    <time datetime="${esc(message.created_at)}">${esc(formatDate(message.created_at))}</time>${rewind}</div>
    ${editor ? renderRewindEditor(editor, busy) : '<div class="message-text">' + esc(message.content) + '</div>'}
    ${thought}${refs ? '<div class="references">' + refs + '</div>' : ''}${attribution}</article>`;
}

function renderThread(messages, page, { canRewind, editor, busy } = {}, allMessages = messages) {
  const byId = new Map(allMessages.map(message => [message.id, message]));
  return messages.map(message => renderMessage(message, {
    page,
    canRewind,
    editor: editor?.messageId === message.id ? editor : null,
    busy,
    highlighted: message.id === state.route.message,
    showId: page === 'memory',
    replyTarget: byId.get(message.reply_to),
  })).join('');
}

function isChatWaiting(detail) {
  return state.pending.has(detail?.session.id ?? 'new') || Boolean(detail?.chat_active);
}

function isComposerLocked(detail) {
  return isChatWaiting(detail) || state.rewindEditors.has(detail?.session.id);
}

function canStartRewind(detail) {
  const session = detail?.session;
  return session && !session.archived && !isComposerLocked(detail) && !state.summaryJobs.has(session.id);
}

function renderChatFeed(detail) {
  const session = detail?.session;
  const draftKey = session?.id ?? 'new';
  const busy = isChatWaiting(detail);
  const editor = state.rewindEditors.get(draftKey);
  const messages = detail?.messages ?? [];
  const publicMessages = messages.filter(message => message.channel !== 'aside');
  const chatErrors = state.chatErrors.get(draftKey) ?? [];
  const canRewind = canStartRewind(detail);
  const thread = publicMessages.length ? '<div class="thread">' + renderThread(publicMessages, 'chat', { canRewind, editor, busy }, messages) + '</div>'
    : `<section class="chat-empty">
      <div class="chat-empty-copy"><h2>你今天在想什么？</h2><p>半句话也可以发。</p></div>
      <figure class="chat-cover"><img src="/static/images/window-notebook.jpg" alt="" width="1536" height="1024" decoding="async"></figure>
    </section>`;
  return `${thread}
    ${busy ? '<p class="status-text" role="status">正在等群友接话…</p>' : ''}
    ${chatErrors.length ? '<div class="inline-error" role="alert">' + chatErrors.map(item =>
      '<p>' + esc([item.bot_name, item.detail].filter(Boolean).join(' · ')) + '</p>').join('') + '</div>' : ''}`;
}

function renderChatSide(detail) {
  const session = detail?.session;
  const messages = detail?.messages ?? [];
  const comments = messages.filter(message => message.channel === 'aside');
  const turn = messages.findLast(message => message.role === 'user');
  const targets = turn ? messages.filter(message => message.id === turn.id || message.turn_id === turn.id) : [];
  const target = targets.find(message => message.id === state.sideTargetId)
    ?? targets.findLast(message => message.channel !== 'aside');
  const locked = isComposerLocked(detail);
  const sidePending = state.sideJobs.has(session?.id);
  const busy = isChatWaiting(detail) || sidePending;
  const guests = enabledBots().filter(bot => bot.id !== target?.bot_id);
  const canInvite = session && !session.archived && target && enabledBots().length;
  const canStop = session && !session.archived && detail.side_active;
  return `<div class="chat-side-header">
    <h2 id="chat-side-heading">交头接耳</h2>
    ${comments.length ? '<span class="small muted">' + comments.length + ' 条</span>' : ''}
  </div>
  ${comments.length ? '<div class="thread side-thread">' + renderThread(comments, 'chat', {}, messages) + '</div>'
    : !detail?.side_active && !sidePending && !detail?.side_error ? '<p class="chat-side-empty">受邀角色的回复会出现在这里。</p>' : ''}
  ${sidePending ? '<p class="status-text side-status" role="status">正在处理侧栏请求…</p>'
    : detail?.side_active ? `<p class="status-text side-status" role="status">群友正在接话。${locked ? '' : '你可以随时发新消息。'}</p>`
    : detail?.side_error ? '<p class="inline-error" role="alert">这次接话没完成，可以再邀请一次。</p>' : ''}
  ${canInvite || canStop ? `<div class="side-tools">
    ${canInvite ? `<label class="side-target-label">接哪句话
      <select name="side-target" aria-label="接哪句话" ${busy || detail.side_active ? 'disabled' : ''}>
        ${targets.map(m => `<option value="${Number(m.id)}" ${m.id === target.id ? 'selected' : ''}>${esc(m.bot_name || '我')} · ${esc(m.content.slice(0, 32))}</option>`).join('')}
      </select></label>` : ''}
    ${canInvite && guests.length ? `<select name="side-guest" class="side-guest-select" aria-label="请人插嘴" data-message-id="${Number(target.id)}" ${busy || detail.side_active ? 'disabled' : ''}>
      <option value="">请人插嘴</option>${guests.map(bot => `<option value="${Number(bot.id)}">${esc(bot.name)}</option>`).join('')}
    </select>` : ''}
    ${canStop ? button('停一下', 'stop-side', busy ? 'disabled' : '') : ''}
  </div>` : ''}`;
}

function renderChat() {
  const detail = state.detail;
  const session = detail?.session;
  const draftKey = session?.id ?? 'new';
  const locked = isComposerLocked(detail);
  const bots = enabledBots();
  const mirror = bots.find(bot => bot.role_mode === 'mirror');
  const actions = session ? `<a class="btn" href="${href('memory', session.id)}">会话记忆</a>` : '';
  return `<div class="chat-page">${header(session?.title || '新对话', actions)}
    <div class="page-body chat-body">
    <div id="chat-feed" class="chat-feed">${renderChatFeed(detail)}</div>
    <aside id="chat-side" class="chat-side" aria-labelledby="chat-side-heading">${renderChatSide(detail)}</aside>
    ${session?.archived ? `<div class="composer-wrap"><div class="composer archive-notice">
      <span>该会话已归档。</span><a class="btn" href="${href('memory', session.id)}">到会话记忆恢复</a>
    </div></div>` : `<div class="composer-wrap"><form class="composer" data-form="chat">
      <fieldset ${locked ? 'disabled' : ''}>
        <textarea name="content" maxlength="20000" placeholder="写下一个想法…" aria-label="你的想法" required>${esc(state.drafts.get(draftKey) || '')}</textarea>
        <div class="composer-footer">
          <div class="composer-tools">${bots.length ?
            `<select name="mention" class="mention-select" aria-label="点名机器人">
              <option value="">@ 点名</option>${bots.map(bot => `<option value="${Number(bot.id)}">${esc(bot.name)}</option>`).join('')}
            </select>` : '<a class="btn" href="#ops/bots">去配置机器人</a>'}</div>
          <div class="button-row">
            ${mirror ? button('问' + esc(mirror.name), 'mention-mirror', 'data-bot-id="' + Number(mirror.id) + '"', 'text') : ''}
            <button class="btn primary composer-send" type="submit">发送</button>
          </div>
        </div>
      </fieldset>
    </form></div>`}
    </div></div>`;
}

function summaryItems(items, sessionId) {
  return items?.length ? '<ul>' + items.map(item => {
    const refs = (item.source_ids || []).map(id => referenceLink({session_id: sessionId, message_id: id}, '原话 #' + id)).join('');
    return '<li>' + esc(item.text) + (refs ? '<div class="references">' + refs + '</div>' : '') + '</li>';
  }).join('') + '</ul>' : '<p class="muted small">暂无</p>';
}

function summaryCard(summary, sessionId) {
  if (!summary) return '<p class="muted small">生成后可以编辑会话总结。</p>';
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
    <div class="button-row">${button('编辑总结', 'edit-summary')}${button('删除总结', 'delete-summary', '', 'danger')}</div>`;
}

function modelOptions(selected, models = state.config.models) {
  return models.map(model => `<option value="${model.id}" ${selected === model.id ? 'selected' : ''}>${esc(model.name)}</option>`).join('');
}

function renderObservations(detail) {
  const observations = detail.observations ?? [];
  const names = {
    fact: '事实', time: '时间', mood: '情绪', value: '价值取向', concern: '顾虑', stance: '看法',
    style: '表达方式', relation: '关联',
  };
  const count = detail.observer_pending;
  return `<div class="card-header"><h2>观察切片</h2>${count ? '<span class="tag tag--pending">整理中</span>' : ''}</div>
    ${observations.length ? `<div class="observation-list">${observations.map(observation => {
      const source = Number(observation.source_message_id);
      const related = Number(observation.related_message_id);
      const relatedSession = Number(observation.related_session_id || observation.related_source_session_id);
      const note = observation.certainty === 'explicit' ? '明确表达' : '待确认的推测';
      const title = observation.relation === 'contrast' ? '前后差异' : names[observation.kind] ?? '观察';
      return `<article class="observation-note">
        <div class="tags"><span class="tag">${esc(title)}</span>
        <span class="small muted">${note}</span>
        <time class="small muted" datetime="${esc(observation.created_at)}">${esc(formatDate(observation.created_at))}</time></div>
        <p>${esc(observation.text)}</p>
        ${observation.evidence ? '<blockquote>' + esc(observation.evidence) + '</blockquote>' : ''}
        <div class="references">
          ${source ? referenceLink({session_id: detail.session.id, message_id: source}, '原话 #' + source) : ''}
          ${related && relatedSession ? referenceLink({session_id: relatedSession, message_id: related}, '相关原话 #' + related) : ''}
        </div></article>`;
    }).join('')}</div>` : '<p class="muted small">聊天时会从你的原话整理相关想法。</p>'}`;
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
      <div class="thread">${messages.length ? renderThread(messages, 'memory') :
      '<p class="muted">这场会话还没有消息。</p>'}</div></section>
    <div class="cards"><section class="card"><div class="card-header"><h2>会话总结</h2></div>
      <label class="checkbox-field memory-toggle"><input type="checkbox" name="share-memory" ${session.summary_share_enabled !== false ? 'checked' : ''}>
      总结和观察切片可供其他会话引用</label>
      <form class="memory-controls" data-form="generate-summary">
        <label class="field"><span>总结模型</span><select name="model_id" ${!enabled.length || pending ? 'disabled' : ''}>
        ${enabled.length ? modelOptions(chosen, enabled) : '<option>请先启用模型</option>'}</select></label>
        <button type="submit" class="btn primary" ${!enabled.length || !messages.length || pending ? 'disabled' : ''}>${pending ? '正在生成…' : summary ? '重新生成' : '生成总结'}</button>
      </form>
      ${summaryCard(summary, session.id)}
    </section>
    <section class="card" id="observer-card">${renderObservations(detail)}</section></div>
  </div>`;
}

function configName(collection, id) {
  return state.config[collection].find(row => row.id === id)?.name || '未知配置';
}

function configDescription(tab, item) {
  if (tab === 'providers') return [item.base_url, { deepseek: 'DeepSeek', poe: 'Poe（OpenAI 兼容）' }[item.kind] ?? 'OpenAI 兼容',
    item.has_key ? '已配置密钥' : '未配置密钥'];
  if (tab === 'models') return [configName('providers', item.provider_id), item.model_code, item.enabled ? '已启用' : '已停用'];
  if (tab === 'bots') {
    const model = state.config.models.find(candidate => candidate.id === item.model_id);
    return [model ? configName('providers', model.provider_id) : '未知 API', model?.name ?? '未知模型',
      item.role_mode === 'mirror' ? '由观察切片更新' : configName('personas', item.persona_id),
      item.enabled && model?.enabled ? '已启用' : '已停用'];
  }
  return [item.prompt.length > 140 ? item.prompt.slice(0, 140) + '…' : item.prompt];
}

function renderEvolution(botId) {
  const bot = state.config.bots.find(item => item.id === botId);
  const back = '<a class="btn" href="#ops/bots">返回机器人</a>';
  if (!bot) return header('人格演变', back) + '<div class="page-body"><p>找不到这个机器人。</p></div>';
  const { enabled, notes } = state.evolution;
  const names = { view: '看法', interest: '兴趣', habit: '习惯' };
  const current = notes.filter(note => note.active);
  const earlier = notes.filter(note => !note.active);
  const renderNotes = list => list.map(note => `<article class="evolution-note">
    <div class="evolution-note-meta">
      <span class="tag">${esc(names[note.kind] ?? '记录')}</span>
      <time datetime="${esc(note.created_at)}">${esc(formatDate(note.created_at))}</time>
    </div>
    <p>${esc(note.text)}</p>
    ${note.keywords?.length ? '<p class="small muted">' + note.keywords.map(esc).join(' · ') + '</p>' : ''}
    <div class="button-row">
      ${referenceLink({session_id: note.source_session_id, message_id: note.source_message_id}, '来源消息 #' + note.source_message_id)}
      ${button('删除', 'delete-evolution-note', 'data-note-id="' + Number(note.id) + '"', 'text danger')}
    </div>
  </article>`).join('');
  return header(bot.name + '的人格演变', back, '机器人') + `<div class="page-body cards">
    <section class="card">
      ${checkboxField('自动更新人格', 'evolution-enabled', enabled)}
      <p class="status-text">从自己的发言中积累看法、兴趣和习惯。关闭后，已有记录仍可用于聊天。</p>
    </section>
    <section class="card">
      <h2>当前记录</h2>
      ${current.length ? renderNotes(current) : '<p class="muted small">还没有形成可跨会话保留的记录。</p>'}
    </section>
    ${earlier.length ? '<details class="card"><summary>以前的记录</summary>' + renderNotes(earlier) + '</details>' : ''}
  </div>`;
}

function renderOpsTabs(activeTab) {
  const tabs = [...Object.entries(collections).map(([key, item]) => [key, item.name]), ...Object.entries(monitorTabs), ['backup', '备份']];
  return '<nav class="ops-tabs" aria-label="配置分类">' + tabs.map(([key, name]) =>
    `<a href="#ops/${key}" ${key === activeTab ? 'aria-current="page"' : ''}>${name}</a>`).join('') + '</nav>';
}

const outcomeLabels = {
  success: '成功', timeout: '超时', quota: '额度或余额不足', rate_limit: '请求限流', auth: '鉴权失败',
  permission: '权限不足', network: '网络异常', config: '配置异常', upstream: '上游异常',
  invalid_response: '返回异常', cancelled: '已取消',
};
const taskLabels = {
  route: '路由', reply: '主聊', aside_route: '侧栏路由', aside: '侧栏接话',
  reply_retry: '重答', reply_repair: '格式修正',
  observe: '观察器', summary: '总结', test: '连接测试', health: '模型调用测试', web_search_test: '手动搜索',
};
const numberFormat = new Intl.NumberFormat('zh-CN');

function count(value) {
  return typeof value === 'number' && Number.isFinite(value) ? numberFormat.format(value) : '—';
}

function latency(value) {
  return typeof value === 'number' && Number.isFinite(value) ? count(Math.round(value)) + ' ms' : '—';
}

function monitorQuery(tab) {
  const filters = new URLSearchParams();
  for (const [name, value] of Object.entries(state.monitorFilters)) {
    if (value) filters.set(name, value);
  }
  if (tab === 'web') filters.set('web_only', 'true');
  return '/monitor?' + filters;
}

function renderMonitorFilters() {
  const filters = state.monitorFilters;
  const { providers, models } = state.config;
  const select = (name, values, label, optionAll) => formField(label, `<select name="monitor-${name}">
    ${optionAll ? '<option value="">' + esc(optionAll) + '</option>' : ''}
    ${values.map(([value, text]) => `<option value="${esc(value)}" ${String(value) === filters[name] ? 'selected' : ''}>${esc(text)}</option>`).join('')}
    </select>`);
  const matchingModels = models.filter(model => !filters.provider_id || String(model.provider_id) === filters.provider_id);
  return `<div class="monitor-filters">
    ${select('period', [['24h', '最近 24 小时'], ['7d', '最近 7 天'], ['30d', '最近 30 天']], '时间')}
    ${select('provider_id', providers.map(item => [item.id, item.name]), 'API', '全部 API')}
    ${select('model_id', matchingModels.map(item => [item.id,
      item.name + (filters.provider_id ? '' : ' · ' + configName('providers', item.provider_id))]), '模型', '全部模型')}
  </div>`;
}

function monitorStatus(outcome) {
  const kind = outcome === 'success' ? 'success' : outcome === 'cancelled' ? 'neutral' : 'warning';
  return `<span class="monitor-state monitor-state--${kind}">${esc(outcomeLabels[outcome] ?? outcome ?? '未知状态')}</span>`;
}

function monitorSource(row) {
  return `<strong>${esc(row.provider_name || '未知 API')}</strong><span class="monitor-extra">${esc(row.model_name || row.model_code || '未知模型')}</span>`;
}

function renderMonitorTable(title, headings, rows, empty) {
  return `<section class="card monitor-section"><h2>${title}</h2>
    ${rows.length ? `<div class="monitor-table-scroll" role="region" aria-label="${title}" tabindex="0">
      <table class="monitor-table"><thead><tr>${headings.map(heading => '<th scope="col">' + heading + '</th>').join('')}</tr></thead>
      <tbody>${rows.join('')}</tbody></table></div>` : '<p class="muted">' + empty + '</p>'}</section>`;
}

function renderUsageMetrics(data) {
  const { totals, breakdown, recent } = data;
  const metrics = [
    ['调用次数', count(totals.calls), `成功 ${count(totals.successes)} · 失败 ${count(totals.failures)} · 取消 ${count(totals.cancelled)}`],
    ['报告的 Token', totals.usage_reports ? count(totals.total_tokens) : '—',
      totals.usage_reports ? `输入 ${count(totals.input_tokens)} · 输出 ${count(totals.output_tokens)} · ${count(totals.usage_reports)} 次有用量` : '暂无用量返回'],
    ['平均耗时', latency(totals.avg_latency_ms), ''],
  ];
  return `<div class="monitor-metrics">${metrics.map(([label, value, note]) => `<section class="card monitor-metric">
    <h2>${label}</h2><strong>${value}</strong>${note ? '<p class="muted small">' + note + '</p>' : ''}</section>`).join('')}</div>
    ${renderMonitorTable('按 API 和模型', ['API / 模型', '调用', '失败', '报告的 Token', '平均耗时'],
      breakdown.map(row => `<tr><td>${monitorSource(row)}</td><td>${count(row.calls)}</td><td>${count(row.failures)}</td>
        <td>${row.usage_reports ? count(row.total_tokens) : '—'}</td><td>${latency(row.avg_latency_ms)}</td></tr>`), '这个时间范围还没有 API 调用。')}
    ${renderMonitorTable('最近调用', ['时间', 'API / 模型', '用途', '结果', '耗时', '报告的 Token'],
      recent.map(row => `<tr><td><time datetime="${esc(row.created_at)}">${esc(formatDate(row.created_at))}</time></td>
        <td>${monitorSource(row)}</td><td>${esc(taskLabels[row.task] ?? row.task ?? '调用')}
        ${row.bot_name ? '<span class="monitor-extra">' + esc(row.bot_name) + '</span>' : ''}
        ${row.web_enabled ? `<span class="monitor-extra">${webCounts(row)}</span>` : ''}</td>
        <td>${monitorStatus(row.outcome)}</td><td>${latency(row.latency_ms)}</td><td>${count(row.total_tokens)}</td></tr>`), '没有符合筛选条件的调用。')}`;
}

function renderAlerts(data) {
  return data.alerts.length ? data.alerts.map(alert => {
    const acknowledged = alert.acknowledged_at;
    const seen = `最近 ${formatDate(alert.last_seen)} · 首次 ${formatDate(alert.first_seen)}`;
    return `<article class="card monitor-alert">
      <div><div class="monitor-alert-heading">${monitorStatus(alert.outcome)}
      <span class="small muted">${count(alert.count)} 次</span>
      ${alert.http_status ? '<span class="small muted">HTTP ' + count(alert.http_status) + '</span>' : ''}</div>
      <p>${monitorSource(alert)}</p>
      ${alert.detail ? '<p>' + esc(alert.detail) + '</p>' : ''}
      <p class="muted small">${esc(seen)}${acknowledged ? ' · 已确认 ' + esc(formatDate(acknowledged)) : ''}</p></div>
      ${acknowledged ? '' : button('确认告警', 'ack-alert', 'data-alert-id="' + Number(alert.id) + '"')}
    </article>`;
  }).join('') : '<section class="card monitor-section"><p>这个时间范围没有 API 告警。</p></section>';
}

function webCounts(record) {
  if (record.calls === 0) return '搜索 0 次 · 读页 0 次';
  if (record.search_count == null && record.read_count == null) return '用量未知';
  return `搜索 ${count(record.search_count)} 次 · 读页 ${count(record.read_count)} 次`;
}

function renderWebSearch(data) {
  const experts = state.config.bots.filter(bot => bot.can_web_search);
  const job = state.webSearch;
  const result = job.result;
  return `<section class="card monitor-section"><h2>通过专家联网查询</h2>
    <p class="status-text">聊天时，点名专家或由路由选中专家后，DeepSeek 会根据问题决定是否搜索。提供网页链接时，也可按需读取。普通群友不调用这两个工具。</p>
    <p class="small muted wrap">本地接口 <code>POST /api/config/bots/{bot_id}/web-search</code></p>
    <p class="small muted wrap">上游接口 <code>POST https://api.deepseek.com/anthropic/v1/messages</code></p>
    ${experts.length ? `<form class="form-grid" data-form="web-search">
      ${selectField('专家', 'bot_id', experts, Number(job.botId) || experts[0].id)}
      ${textareaField('搜索问题或网页链接', 'query', job.query, 'required maxlength="3000" rows="2"')}
      <div><button class="btn primary" type="submit" ${job.busy ? 'disabled' : ''}>${job.busy ? '查询中…' : '联网查询'}</button></div>
    </form>` : '<p>请在「机器人」启用专家，并为其配置有密钥的官方 DeepSeek Flash 模型。</p>'}
    ${job.error ? `<p class="inline-error" role="alert">${esc(job.error)}</p>` : ''}
    ${result ? `<p class="small">${webCounts(result)}</p><div class="message-text">${esc(result.answer)}</div>
      ${result.sources.map(source => `<p><a href="${esc(source.url)}" target="_blank" rel="noreferrer">${esc(source.title)}</a></p>`).join('')}` : ''}
    </section>
    <section class="card"><h2>工具用量</h2><p>${count(data.totals.calls)} 次可联网请求 · ${webCounts(data.totals)}</p>
    <p class="small muted">总数只累计有回执的用量。每次请求中，零次表示未使用，未知表示缺少回执。工具用量从本次更新开始记录。</p></section>
    ${renderMonitorTable('可联网请求', ['时间', '专家', '用途', '工具用量', '请求结果', '请求耗时'],
       data.recent.map(row => `<tr><td>${esc(formatDate(row.created_at))}</td><td>${esc(row.bot_name)}</td>
         <td>${esc(taskLabels[row.task] || row.task)}</td><td>${webCounts(row)}</td>
         <td>${monitorStatus(row.outcome)}</td><td>${latency(row.latency_ms)}</td></tr>`), '当前筛选条件下还没有可联网请求。')}`;
}

async function submitWebSearch(form) {
  const job = state.webSearch;
  if (job.busy) return;
  job.query = form.elements.query.value.trim();
  if (!job.query) return;
  job.botId = form.elements.bot_id.value;
  Object.assign(job, { busy: true, result: null, error: '' });
  render();
  try {
    job.result = await api('/config/bots/' + job.botId + '/web-search', 'POST', { query: job.query });
  } catch (error) {
    job.error = error.message;
  } finally {
    job.busy = false;
    if (state.route.page === 'ops' && state.route.tab === 'web') await load({ keepScroll: true });
  }
}

function renderMonitor(tab) {
  const views = { alerts: renderAlerts, web: renderWebSearch, monitor: renderUsageMetrics };
  const content = views[tab](state.monitor);
  return header(monitorTabs[tab], button('刷新', 'reload')) + `<div class="page-body">
    ${renderOpsTabs(tab)}${renderMonitorFilters()}
    <div class="cards monitor-stack">${content}</div></div>`;
}

function renderReplyModels() {
  const models = state.config.models.filter(model => model.enabled);
  const selector = (label, key, defaultLabel) => formField(label, `<select name="${key}">
    <option value="">${defaultLabel}</option>
    ${models.map(model => `<option value="${Number(model.id)}" ${state.config[key] === model.id ? 'selected' : ''}>
      ${esc(model.name)} · ${esc(configName('providers', model.provider_id))}</option>`).join('')}
    </select>`);
  return `<section class="card"><h2>路由与回退</h2>
    <form data-form="reply-models">
      <fieldset class="reply-model-fields">
        <div>${selector('路由模型', 'router_model_id', '自动选择')}
          <p class="small muted">分发主聊和侧栏接话，需要重答时替原角色重新生成。</p></div>
        <div>${selector('回退模型', 'fallback_model_id', '不启用')}
          <p class="small muted">回复格式不合规时，尝试修正 JSON。</p></div>
        <button type="submit" class="btn primary">保存设置</button>
      </fieldset>
    </form></section>`;
}

function renderModelTestResult(result) {
  if (!result || result.pending) return '';
  const metrics = [
    ['耗时', latency(result.latency_ms), result.latency_ms],
    ['HTTP', result.http_status, result.http_status],
    ['输入 Token', count(result.input_tokens), result.input_tokens],
    ['输出 Token', count(result.output_tokens), result.output_tokens],
    ['总 Token', count(result.total_tokens), result.total_tokens],
  ];
  return `${monitorStatus(result.outcome)}
    ${metrics.filter(([, , value]) => typeof value === 'number').map(([label, value]) => `<span>${label} ${value}</span>`).join('')}
    ${!result.ok && result.message ? '<p class="model-test-error">' + esc(result.message) + '</p>' : ''}`;
}

function renderModelTestButton(id) {
  const pending = state.modelTests.get(id)?.pending;
  return button(pending ? '测试中…' : '测试调用', 'test-model',
    `data-id="${Number(id)}"${pending ? ' disabled aria-busy="true"' : ''}`);
}

async function testModel(id) {
  if (state.modelTests.get(id)?.pending) return;
  const pending = { pending: true };
  const update = result => {
    state.modelTests.set(id, result);
    if (state.route.page !== 'ops' || state.route.tab !== 'models') return;
    const control = main.querySelector(`[data-action="test-model"][data-id="${id}"]`);
    if (!control) return;
    control.disabled = Boolean(result.pending);
    control.textContent = result.pending ? '测试中…' : '测试调用';
    if (result.pending) control.setAttribute('aria-busy', 'true');
    else control.removeAttribute('aria-busy');
    control.closest('.ops-card').querySelector('[data-model-test-result]').innerHTML = renderModelTestResult(result);
  };
  update(pending);
  let result;
  try {
    result = await api('/config/models/' + id + '/test', 'POST');
  } catch (error) {
    result = { ok: false, outcome: 'upstream', message: error.message };
  }
  if (state.modelTests.get(id) === pending) update(result);
}

async function saveReplyModels(form) {
  const fields = form.querySelector('fieldset');
  if (fields.disabled) return;
  const body = Object.fromEntries([...new FormData(form)].map(([key, value]) => [key, value ? Number(value) : null]));
  fields.disabled = true;
  try {
    const settings = await api('/config/reply-models', 'PATCH', body);
    Object.assign(state.config, settings);
    if (form.isConnected) {
      for (const [key, id] of Object.entries(settings)) form.elements[key].value = id ?? '';
      flash('已保存路由与回退模型');
    }
  } catch (error) {
    flash(error.message, true);
  } finally {
    fields.disabled = false;
  }
}

function renderOps() {
  const { tab, botId } = state.route;
  if (botId) return renderEvolution(botId);
  if (Object.hasOwn(monitorTabs, tab)) return renderMonitor(tab);
  if (tab === 'backup') {
    return header('备份与恢复') + `<div class="page-body">${renderOpsTabs(tab)}${backupPage.render()}</div>`;
  }
  const collection = collections[tab];
  const items = state.config[tab];
  const cards = items.map(item => `<article class="card ops-card">
    <div><h2>${esc(item.name)}</h2><p class="small muted wrap">${configDescription(tab, item).map(esc).join(' · ')}</p>
      ${tab === 'models' ? `<div class="model-test-result small" role="status" data-model-test-result>${renderModelTestResult(state.modelTests.get(item.id))}</div>` : ''}</div>
    <div class="button-row">
      ${tab === 'bots' ? `<label class="checkbox-field"><input type="checkbox" name="bot-enabled" data-bot-id="${Number(item.id)}"
        aria-label="启用${esc(item.name)}" ${item.enabled ? 'checked' : ''}>启用</label>` : ''}
      ${tab === 'models' ? renderModelTestButton(item.id) : ''}
      ${button('编辑', 'edit-config', 'data-id="' + item.id + '"')}
      ${tab === 'bots' ? (item.role_mode === 'mirror'
        ? '<a class="btn" href="#memory">查看观察切片</a>'
        : `<a class="btn" href="#ops/bots/${item.id}">人格演变</a>`) : ''}
      ${tab === 'personas' ? button('复制', 'copy-config', 'data-id="' + item.id + '"') : ''}
      ${button('删除', 'delete-config', 'data-id="' + item.id + '"', 'danger')}
    </div>
  </article>`).join('');
  return header('运维', button('＋ 添加' + collection.item, 'add-config', '', 'primary')) +
    `<div class="page-body">${renderOpsTabs(tab)}
    <div class="cards">${tab === 'bots' ? `<section class="card">
      ${checkboxField('长回答', 'long-replies', state.config.long_replies)}
      <p class="status-text">默认按话题调整长短。开启后，主聊和交头接耳都会更充分地展开观点。</p>
    </section>` : tab === 'models' ? renderReplyModels() : ''}${cards || '<section class="card empty-card"><h2>还没有' + collection.item + '</h2><p>' + collection.empty + '</p>' + button('添加' + collection.item, 'add-config', '', 'primary') + '</section>'}</div></div>`;
}

function render() {
  renderSidebar();
  if (state.failed) {
    main.innerHTML = header('想法簿') + '<div class="page-body"><div class="inline-error" role="alert">' +
      esc(state.failed) + '</div><p class="status-text">' + button('重新加载', 'reload') + '</p></div>';
  } else if (state.config) {
    main.innerHTML = state.route.page === 'ops' ? renderOps() : state.route.page === 'memory' ? renderMemory() : renderChat();
    if (state.route.page === 'ops' && state.route.tab === 'backup') backupPage.mount();
  }
  main.removeAttribute('aria-busy');
  main.inert = false;
}

async function load({ keepScroll = false } = {}) {
  clearTimeout(state.pollTimer);
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
    const [detail, evolution, monitor] = await Promise.all([
      route.page !== 'ops' && route.id ? api('/sessions/' + route.id) : null,
      route.botId && config.bots.some(bot => bot.id === route.botId) ? api('/config/bots/' + route.botId + '/evolution') : null,
      route.page === 'ops' && Object.hasOwn(monitorTabs, route.tab) ? api(monitorQuery(route.tab)) : null,
    ]);
    if (loadId !== state.loadId) return;
    state.config = config;
    state.sessions = sessions;
    state.detail = detail ?? null;
    state.evolution = evolution;
    state.monitor = monitor;
    reconcileRewindEditor(detail);
    if (route.page === 'chat' && typeof detail?.rewind_draft === 'string'
        && !state.drafts.has(route.id) && !isChatWaiting(detail)) {
      state.drafts.set(route.id, detail.rewind_draft);
    }
    state.failed = '';
    render();
    if (route.message) document.querySelector('#message-' + route.message)?.scrollIntoView({block: 'start'});
    else if (!keepScroll) {
      if (route.page === 'chat' && detail?.messages.length) window.scrollTo(0, document.body.scrollHeight);
      else window.scrollTo(0, 0);
    }
    schedulePoll();
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

function selectField(label, name, items, selected, placeholder) {
  return formField(label, `<select name="${name}" required>
    ${placeholder ? '<option value="">' + esc(placeholder) + '</option>' : ''}
    ${items.length ? items.map(item => `<option value="${esc(item.id)}" ${selected === item.id ? 'selected' : ''}>${esc(item.name)}</option>`).join('')
    : placeholder ? '' : '<option value="">暂无配置</option>'}</select>`);
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
  let initialProvider;
  if (tab === 'providers') {
    fields += textField('接口地址', 'base_url', item?.base_url, 'type="url" maxlength="1024" required placeholder="https://api.deepseek.com"');
    fields += selectField('协议', 'kind', [
      {id: 'openai', name: 'OpenAI Chat Completions 兼容'}, {id: 'deepseek', name: 'DeepSeek'}, {id: 'poe', name: 'Poe（OpenAI 兼容）'},
    ], item?.kind);
    fields += textField('API 密钥', 'api_key', '', 'type="password" autocomplete="off" maxlength="4096"' +
      (item?.has_key ? ' placeholder="留空则保留已配置密钥"' : ' placeholder="输入密钥"'));
  } else if (tab === 'models') {
    fields += selectField('所属 API', 'provider_id', state.config.providers, item?.provider_id);
    fields += textField('模型 ID', 'model_code', item?.model_code, 'maxlength="200" required placeholder="deepseek-flash"');
    fields += checkboxField('启用模型', 'enabled', item?.enabled ?? true);
  } else if (tab === 'bots') {
    initialProvider = state.config.models.find(model => model.id === item?.model_id)?.provider_id;
    fields += selectField('API', 'bot_provider_id', state.config.providers, initialProvider, '选择 API');
    fields += selectField('模型', 'model_id', [], null, '先选择 API');
    fields += selectField('人格模板', 'persona_id', state.config.personas, item?.persona_id);
    fields += selectField('角色类型', 'role_mode', [
      {id: 'friend', name: '朋友'}, {id: 'mirror', name: '影子自我'},
    ], item?.role_mode ?? 'friend');
    fields += checkboxField('启用机器人', 'enabled', item?.enabled ?? true);
  } else {
    fields += textareaField('角色设定', 'prompt', item?.prompt, 'rows="6" maxlength="12000" required');
  }

  const modal = dialog((editing ? '编辑' : copy ? '复制' : '添加') + collections[tab].item, fields, async data => {
    const body = Object.fromEntries(data);
    delete body.bot_provider_id;
    for (const key of ['provider_id', 'model_id', 'persona_id']) {
      if (Object.hasOwn(body, key)) body[key] = Number(body[key]);
    }
    if (tab === 'bots' || tab === 'models') body.enabled = data.has('enabled');
    if (tab === 'providers' && !body.api_key.trim()) delete body.api_key;
    await api('/config/' + tab + (editing ? '/' + item.id : ''), editing ? 'PATCH' : 'POST', body);
    if (editing && tab === 'models') state.modelTests.delete(item.id);
    if (editing && tab === 'providers') {
      for (const model of state.config.models) {
        if (model.provider_id === item.id) state.modelTests.delete(model.id);
      }
    }
    await load({ keepScroll: true });
  });
  if (tab === 'bots') {
    const { bot_provider_id: provider, model_id: model } = modal.querySelector('form').elements;
    const update = selected => {
      const models = state.config.models.filter(candidate => candidate.provider_id === Number(provider.value)
        && (candidate.enabled || candidate.id === item?.model_id));
      const hint = !provider.value ? '先选择 API' : models.length ? '选择模型' : '该 API 暂无可用模型';
      model.replaceChildren(new Option(hint, ''));
      for (const candidate of models) {
        const label = candidate.name + (candidate.enabled ? '' : ' · 已停用');
        model.add(new Option(label, candidate.id, false, candidate.id === selected));
      }
    };
    update(item?.model_id);
    provider.addEventListener('change', () => update());
  }
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

function reconcileRewindEditor(detail) {
  if (!detail) return;
  const id = detail.session.id;
  const editor = state.rewindEditors.get(id);
  if (editor && (detail.session.archived || !detail.messages.some(message => message.id === editor.messageId))) {
    state.rewindEditors.delete(id);
  }
}

function updateChatFeed() {
  const feed = main.querySelector('#chat-feed');
  if (!feed) return;
  const active = document.activeElement;
  const editing = feed.contains(active) && active.name === 'edited_content';
  const selection = editing && [active.selectionStart, active.selectionEnd, active.selectionDirection];
  const scrollTop = editing && active.scrollTop;
  feed.innerHTML = renderChatFeed(state.detail);
  if (selection) {
    const input = feed.querySelector('[name="edited_content"]:enabled');
    input?.focus({ preventScroll: true });
    input?.setSelectionRange(...selection);
    if (input) input.scrollTop = scrollTop;
  }
}

function updateComposerLock() {
  const fields = main.querySelector('[data-form="chat"] fieldset');
  if (fields) fields.disabled = isComposerLocked(state.detail);
}

function updateSidePanel() {
  const panel = main.querySelector('#chat-side');
  if (!panel) return;
  const previous = panel.querySelector('.side-thread');
  const offset = previous?.scrollTop ?? 0;
  const nearEnd = !previous || offset + previous.clientHeight >= previous.scrollHeight - 24;
  panel.innerHTML = renderChatSide(state.detail);
  const thread = panel.querySelector('.side-thread');
  if (thread) thread.scrollTop = nearEnd ? thread.scrollHeight : offset;
}

async function refreshConversation(sessionId) {
  const loadId = state.loadId;
  const page = state.route?.page;
  try {
    const detail = await api('/sessions/' + sessionId);
    if (loadId !== state.loadId || !isViewing(sessionId, page)) return;
    const previous = state.detail;
    state.detail = detail;
    reconcileRewindEditor(detail);
    if (page === 'chat') {
      const oldMessages = previous?.messages ?? [];
      const visible = message => message.channel !== 'aside';
      const mainChanged = detail.messages.findLast(visible)?.id !== oldMessages.findLast(visible)?.id;
      const waitingChanged = detail.chat_active !== previous?.chat_active;
      if (mainChanged || waitingChanged) {
        const nearBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 150;
        updateChatFeed();
        updateComposerLock();
        if (nearBottom && !state.rewindEditors.has(sessionId)) window.scrollTo(0, document.body.scrollHeight);
      }
      const messagesChanged = detail.messages.length !== oldMessages.length
        || detail.messages.at(-1)?.id !== oldMessages.at(-1)?.id;
      if (messagesChanged || waitingChanged || detail.side_active !== previous?.side_active || detail.side_error !== previous?.side_error) {
        updateSidePanel();
      }
    } else if (page === 'memory') {
      const card = main.querySelector('#observer-card');
      if (card) card.innerHTML = renderObservations(detail);
    }
  } catch {
    // An intermittent polling failure leaves the visible session intact.
  }
}

function schedulePoll() {
  clearTimeout(state.pollTimer);
  const { id, page } = state.route ?? {};
  const shouldPoll = page === 'chat'
    ? isChatWaiting(state.detail) || state.sideJobs.has(id) || state.detail?.side_active
    : page === 'memory' && state.detail?.observer_pending;
  if (!id || !shouldPoll) return;
  const loadId = state.loadId;
  state.pollTimer = setTimeout(async () => {
    await refreshConversation(id);
    if (loadId === state.loadId) schedulePoll();
  }, 950);
}

async function sendChat(form) {
  const rewindMessageId = form.dataset.form === 'rewind-chat' ? Number(form.dataset.messageId) : null;
  const draft = (rewindMessageId ? form.elements.edited_content : form.elements.content).value;
  const content = draft.trim();
  const detail = state.detail;
  const session = detail?.session;
  let sessionId = session?.id;
  let draftKey = sessionId ?? 'new';
  if (!content || session?.archived || isChatWaiting(detail)) return;
  const editor = state.rewindEditors.get(sessionId);
  if (rewindMessageId ? editor?.messageId !== rewindMessageId || state.summaryJobs.has(sessionId) : editor) return;
  const previousHash = location.hash;
  state.pending.set(draftKey, true);
  state.chatErrors.delete(draftKey);
  if (editor) editor.content = draft;
  updateChatFeed();
  updateComposerLock();
  updateSidePanel();
  schedulePoll();

  let saved = false;
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
    if (!rewindMessageId) state.drafts.delete(draftKey);
    const body = rewindMessageId ? { content, rewind_message_id: rewindMessageId } : { content };
    const response = await api('/sessions/' + sessionId + '/chat', 'POST', body);
    saved = true;
    if (rewindMessageId) {
      state.rewindEditors.delete(sessionId);
      if (isViewing(sessionId, 'chat') && state.route.message) history.replaceState(null, '', href('chat', sessionId));
    }
    if (response.errors?.length) {
      state.chatErrors.set(sessionId, response.errors);
    } else if (!response.messages?.some(message => message.role === 'assistant') && isViewing(sessionId, 'chat')) {
      flash('已记下');
    }
  } catch (error) {
    if (!rewindMessageId) state.drafts.set(draftKey, draft);
    state.chatErrors.set(draftKey, [{ detail: error.message }]);
    flash(error.message, true);
  } finally {
    state.pending.delete(draftKey);
    if (isViewing(sessionId, 'chat') || (!sessionId && location.hash === previousHash)) {
      await load({ keepScroll: Boolean(rewindMessageId && !saved) });
      if (rewindMessageId && !saved && isViewing(sessionId, 'chat')) focusRewindEditor();
    } else if (rewindMessageId && isViewing(sessionId, 'memory')) {
      await load({ keepScroll: true });
    }
  }
}

function insertMention(content, bot) {
  if (!content || content.matches(':disabled') || !bot) return;
  const start = content.selectionStart;
  const separator = start > 0 && !/\s/.test(content.value[start - 1]) ? ' ' : '';
  const mention = separator + '@' + bot.name + ' ';
  if (content.value.length - (content.selectionEnd - start) + mention.length > content.maxLength) {
    flash('消息已达到长度上限。', true);
    return;
  }
  content.setRangeText(mention, start, content.selectionEnd, 'end');
  state.drafts.set(state.detail?.session.id ?? 'new', content.value);
  content.focus();
}

async function changeSide(sessionId, operation, body) {
  if (!sessionId || isChatWaiting(state.detail) || state.sideJobs.has(sessionId)) return;
  state.sideJobs.add(sessionId);
  updateSidePanel();
  try {
    await api('/sessions/' + sessionId + '/side' + operation, 'POST', body);
    if (operation === '' && isViewing(sessionId, 'chat') && state.detail?.session.id === sessionId) {
      // Keep polling an admitted invitation even if the first detail fetch fails.
      state.detail = { ...state.detail, side_active: true, side_error: null };
    }
  } finally {
    state.sideJobs.delete(sessionId);
    if (isViewing(sessionId, 'chat')) {
      await refreshConversation(sessionId);
      if (isViewing(sessionId, 'chat')) {
        updateSidePanel();
        schedulePoll();
      }
    }
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
    if (error.status !== 502) flash(error.message, true);
  } finally {
    state.summaryJobs.delete(id);
    if (isViewing(id, 'memory')) await load({ keepScroll: true });
  }
}

function focusRewindEditor() {
  const input = main.querySelector('[name="edited_content"]:enabled');
  if (!input) return;
  input.focus({ preventScroll: true });
  input.setSelectionRange(input.value.length, input.value.length);
  input.scrollIntoView({ block: 'nearest' });
}

function startRewind(messageId) {
  const detail = state.detail;
  if (state.route.page !== 'chat' || !canStartRewind(detail)) return;
  const message = detail.messages.find(message => message.id === messageId && message.role === 'user');
  if (!message) return;
  state.rewindEditors.set(detail.session.id, { messageId, content: message.content });
  updateChatFeed();
  updateComposerLock();
  updateSidePanel();
  focusRewindEditor();
}

function cancelRewind() {
  const id = state.detail?.session.id;
  const editor = state.rewindEditors.get(id);
  if (!editor || isChatWaiting(state.detail)) return;
  state.rewindEditors.delete(id);
  updateChatFeed();
  updateComposerLock();
  updateSidePanel();
  main.querySelector(`#message-${editor.messageId} .message-rewind`)?.focus({ preventScroll: true });
}

async function dispatchAction(control) {
  const action = control.dataset.action;
  const session = state.detail?.session;
  const path = session ? '/sessions/' + session.id : null;
  const tab = state.route.tab;
  const item = tab ? state.config[tab]?.find(item => item.id === Number(control.dataset.id)) : null;
  if (action === 'new-chat') {
    if (location.hash === '#chat/new') main.querySelector('textarea')?.focus();
  } else if (action === 'mention-mirror') {
    const bot = enabledBots().find(candidate => candidate.id === Number(control.dataset.botId));
    insertMention(control.closest('form')?.elements.content, bot);
  } else if (action === 'stop-side') {
    await changeSide(session?.id, '/stop', {});
  } else if (action === 'rewind-chat') {
    startRewind(Number(control.dataset.messageId));
  } else if (action === 'cancel-rewind') {
    cancelRewind();
  } else if (action === 'reload') {
    await load();
  } else if (action === 'ack-alert') {
    const alertId = Number(control.dataset.alertId);
    control.disabled = true;
    try {
      await api('/monitor/alerts/' + alertId + '/ack', 'POST');
      if (state.route.tab === 'alerts') await load({ keepScroll: true });
    } finally {
      control.disabled = false;
    }
  } else if (action === 'add-config' || action === 'edit-config' || action === 'copy-config') {
    showConfigEditor(tab, item, action === 'copy-config');
  } else if (action === 'delete-config') {
    if (!confirm('删除「' + item.name + '」？')) return;
    await api('/config/' + tab + '/' + item.id, 'DELETE');
    if (tab === 'models') state.modelTests.delete(item.id);
    await load({ keepScroll: true });
    flash('配置已删除');
  } else if (action === 'test-model' && item) {
    await testModel(item.id);
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
  } else if (action === 'delete-evolution-note') {
    if (!confirm('删除这条人格记录？')) return;
    const botId = state.route.botId;
    const noteId = Number(control.dataset.noteId);
    control.disabled = true;
    try {
      await api('/config/bots/' + botId + '/evolution/' + noteId, 'DELETE');
      if (state.route.botId === botId) await load({ keepScroll: true });
      flash('人格记录已删除');
    } finally {
      control.disabled = false;
    }
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
  if (form.dataset.form === 'chat' || form.dataset.form === 'rewind-chat') {
    event.preventDefault();
    void sendChat(form);
  } else if (form.dataset.form === 'web-search') {
    event.preventDefault();
    void submitWebSearch(form);
  } else if (form.dataset.form === 'generate-summary') {
    event.preventDefault();
    void generateSummary(form);
  } else if (form.dataset.form === 'reply-models') {
    event.preventDefault();
    void saveReplyModels(form);
  }
});

document.addEventListener('input', event => {
  const input = event.target;
  if (input.name === 'search') {
    state.search = input.value;
    sidebar.querySelector('#session-list').innerHTML = sessionLinks();
  } else if (input.name === 'content') {
    state.drafts.set(state.detail?.session.id ?? 'new', input.value);
  } else if (input.name === 'edited_content') {
    const editor = state.rewindEditors.get(state.detail?.session.id);
    if (editor) editor.content = input.value;
  }
});

document.addEventListener('change', async event => {
  const input = event.target;
  const monitorField = input.name?.startsWith('monitor-') ? input.name.slice(8) : null;
  if (monitorField && Object.hasOwn(state.monitorFilters, monitorField)) {
    state.monitorFilters[monitorField] = input.value;
    if (monitorField === 'provider_id') state.monitorFilters.model_id = '';
    await load({ keepScroll: true });
  } else if (input.name === 'mention') {
    const bot = enabledBots().find(candidate => candidate.id === Number(input.value));
    input.value = '';
    insertMention(input.form.elements.content, bot);
  } else if (input.name === 'side-target') {
    state.sideTargetId = Number(input.value);
    updateSidePanel();
    main.querySelector('[name=side-target]')?.focus();
  } else if (input.name === 'side-guest') {
    const bot_id = Number(input.value);
    const message_id = Number(input.dataset.messageId);
    input.value = '';
    if (bot_id && message_id) {
      try {
        await changeSide(state.detail?.session.id, '', { bot_id, message_id });
      } catch (error) {
        flash(error.message, true);
      }
    }
  } else if (input.name === 'model_id' && input.closest('[data-form="generate-summary"]')) {
    state.summaryModels.set(state.detail.session.id, Number(input.value));
  } else if (input.name === 'bot-enabled') {
    const enabled = input.checked;
    input.disabled = true;
    try {
      await api('/config/bots/' + Number(input.dataset.botId), 'PATCH', { enabled });
      await load({ keepScroll: true });
    } catch (error) {
      input.checked = !enabled;
      flash(error.message, true);
    } finally {
      input.disabled = false;
    }
  } else if (input.name === 'long-replies') {
    const enabled = input.checked;
    input.disabled = true;
    try {
      const saved = await api('/config/reply-preferences', 'PATCH', { long_replies: enabled });
      state.config.long_replies = saved.long_replies;
      flash(enabled ? '已开启长回答' : '已恢复自适应长度');
    } catch (error) {
      input.checked = !enabled;
      flash(error.message, true);
    } finally {
      input.disabled = false;
    }
  } else if (input.name === 'evolution-enabled') {
    const botId = state.route.botId;
    const enabled = input.checked;
    input.disabled = true;
    try {
      await api('/config/bots/' + botId + '/evolution', 'PATCH', { enabled });
      if (state.route.botId === botId) state.evolution.enabled = enabled;
      flash(enabled ? '已开启人格自动更新' : '已暂停人格自动更新');
    } catch (error) {
      input.checked = !enabled;
      flash(error.message, true);
    } finally {
      input.disabled = false;
    }
  } else if (input.name === 'share-memory') {
    const id = state.detail.session.id;
    const checked = input.checked;
    input.disabled = true;
    try {
      const session = await api('/sessions/' + id, 'PATCH', { summary_share_enabled: checked });
      if (isViewing(id, 'memory')) {
        state.detail.session = session;
        if (state.detail.summary) state.detail.summary.share_enabled = checked;
      }
      flash(checked ? '总结和观察切片可供其他会话引用' : '已停止跨会话引用');
    } catch (error) {
      input.checked = !checked;
      flash(error.message, true);
    } finally {
      input.disabled = false;
    }
  }
});

document.addEventListener('toggle', event => {
  const call = Boolean(event.target.dataset?.callKey);
  const id = Number(call ? event.target.dataset.callKey : event.target.dataset?.asideKey);
  const opened = call ? state.openedCalls : state.openedAsides;
  if (id && event.target.isConnected) {
    if (event.target.open) opened.add(id);
    else opened.delete(id);
  }
}, true);

document.addEventListener('keydown', event => {
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing || event.keyCode === 229) return;
  const inlineSubmit = event.target.name === 'edited_content' && (event.metaKey || event.ctrlKey);
  if (event.target.name !== 'content' && !inlineSubmit) return;
  const form = event.target.form;
  const submit = form.querySelector('button[type="submit"]');
  event.preventDefault();
  if (!submit.matches(':disabled')) form.requestSubmit(submit);
});

window.addEventListener('hashchange', () => {
  state.search = '';
  void load();
});
void load();
