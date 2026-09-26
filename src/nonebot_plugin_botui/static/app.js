/* ==========================================================================
 * BotUI · 前端控制台脚本（原生 JS，无框架 / 无构建 / 可离线运行）
 *
 * 目录：
 *   1. 常量与状态
 *   2. 通用工具（时间 / 文本 / DOM / 提示条）
 *   3. 接口请求层
 *   4. 启动流程（令牌 / 元信息 / 主题）
 *   5. 会话列表
 *   6. 消息渲染
 *   7. 打开会话 / 加载历史 / 发送
 *   8. 回复 / @ / 输入框
 *   9. 灯箱与右键菜单
 *  10. 滚动与已读
 *  11. 实时事件（WebSocket）
 *  12. 启动与事件绑定
 * ========================================================================== */
'use strict';

/* ============================ 1. 常量与状态 ============================ */

// 由文档基址推导接口前缀：即使路由前缀变化也无需改代码
const API_BASE = new URL('api/', document.baseURI).href.replace(/\/$/, '');

const TOKEN_KEY = 'botui_token';
const THEME_KEY = 'botui_theme';

const PAGE_LIMIT_FALLBACK = 50;  // 每次拉取消息条数（后端可用 BOTUI_PAGE_SIZE 覆盖）
const MSG_MAX = 1200;       // 前端保留的最大消息数
const BACKOFF_MIN = 1000;   // 断线重连退避下限（毫秒）
const BACKOFF_MAX = 30000;  // 断线重连退避上限
const RESYNC_GAP = 30;      // 事件序号出现这么大的空洞就重新拉一次列表
const RESYNC_STALE = 60;    // 超过这么久没收到过任何消息就主动重同步（秒）
const NEAR_BOTTOM = 80;     // 距底部多少像素内算“贴着底部”

const state = {
  meta: null,               // /meta 结果
  token: '',                // 访问令牌
  gateOpen: false,          // 令牌页是否显示
  gateFrom401: false,       // 令牌页是否由 401 触发

  chats: [],                // 会话列表（按时间倒序）
  chatMap: new Map(),       // key -> 会话
  current: null,            // 当前打开的会话
  messages: [],             // 当前会话消息（时间升序）
  msgMap: new Map(),        // 消息 id -> 消息
  lastRendered: null,       // 已渲染的最后一条消息（用于紧凑排版）
  hasMore: true,            // 是否还有更早的消息
  loadingMore: false,       // 是否正在加载更早的消息
  needsScroll: false,       // 渲染后是否需要滚到底部

  unread: new Map(),        // 会话 key -> 未读数
  pending: new Map(),       // 会话 key -> 未读消息摘要

  search: '',
  searchMode: 'chats',      // 'chats' | 'msgs' —— 搜索范围
  searchHits: [],           // 消息搜索结果
  searching: false,         // 是否正在请求消息搜索
  searchSeq: 0,             // 请求序号，丢弃过期响应
  focusMsgId: null,         // 打开会话后要定位到的消息 id
  started: false,           // 是否已进入正常工作状态
  online: false,            // WebSocket 是否连着
  ws: null,                 // 当前 WebSocket
  wsGen: 0,                 // 连接代数：作废旧连接的回调
  wsTimer: null,            // 重连定时器
  wsFails: 0,
  wsDelay: BACKOFF_MIN,
  resyncLast: 0,            // 上次收到任何消息的时间（毫秒）
  syncing: false,           // 正在重新同步中
  // 每次拉取的消息条数，由 /meta 的 page_size 决定（对应 BOTUI_PAGE_SIZE）
  pageSize: PAGE_LIMIT_FALLBACK,
  since: 0,                 // 事件游标（服务端事件序号，不是时间戳）
  sending: false,

  replyTo: null,            // 待回复消息
  pendingAt: [],            // 待发送的 @ 目标
  members: [],              // 当前会话记录到过的成员（@ 菜单数据源）
  memberMap: new Map(),     // 成员 id -> 成员（用于把 @ 显示成昵称）
  atQuery: '',              // @ 菜单的搜索词
  ctxMsg: null,             // 右键菜单目标消息
  sep: { day: '', time: 0 },// 分隔符状态
  newCount: 0               // 未读新消息数（悬浮按钮）
};

// DOM 引用
const $ = (id) => document.getElementById(id);
const el = {
  app: $('app'),
  chatList: $('chatList'),
  searchInput: $('searchInput'),
  searchClear: $('searchClear'),
  searchTabs: $('searchTabs'),
  tabChats: $('tabChats'),
  tabMsgs: $('tabMsgs'),
  msgResults: $('msgResults'),
  themeBtn: $('themeBtn'),
  connDot: $('connDot'),
  connText: $('connText'),
  connMeta: $('connMeta'),
  backBtn: $('backBtn'),
  reloadBtn: $('reloadBtn'),
  headAvatar: $('headAvatar'),
  headName: $('headName'),
  headKind: $('headKind'),
  headSub: $('headSub'),
  msgBox: $('msgViewport'),
  msgList: $('msgList'),
  newMsgBtn: $('newMsgBtn'),
  composer: $('composer'),
  replyBar: $('replyBar'),
  replyBarLabel: $('replyBarLabel'),
  replyBarClose: $('replyBarClose'),
  pendingAt: $('pendingAt'),
  atBtn: $('atBtn'),
  atMenu: $('atMenu'),
  input: $('composerInput'),
  sendBtn: $('sendBtn'),
  hint: $('composerHint'),
  lightbox: $('lightbox'),
  lightboxImg: $('lightboxImg'),
  ctxMenu: $('ctxMenu'),
  gate: $('gate'),
  gateForm: $('gateForm'),
  gateInput: $('gateInput'),
  gateDesc: $('gateDesc'),
  toast: $('toast')
};

/* ============================ 2. 通用工具 ============================ */

/** 构建元素：所有文本都走 textContent，从根上杜绝 XSS */
function h(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

/** 仅允许 http/https，拦截 javascript: 等伪协议 */
function safeUrl(url) {
  if (typeof url !== 'string') return '';
  const trimmed = url.trim();
  return /^https?:\/\//i.test(trimmed) ? trimmed : '';
}

function parseTime(value) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : null;
}

/** null / undefined / 空白 一律视为“无值”，界面上不出现 null */
function hasText(value) {
  return value !== null && value !== undefined && String(value).trim() !== '';
}

function pad2(n) { return n < 10 ? '0' + n : String(n); }

function startOfDay(d) { return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime(); }

function dayDiffFromToday(d) {
  return Math.round((startOfDay(new Date()) - startOfDay(d)) / 86400000);
}

/** 会话列表时间：今天 HH:MM / 昨天 / MM-DD / YYYY-MM-DD */
function listTime(seconds) {
  const t = parseTime(seconds);
  if (t === null) return '';
  const d = new Date(t * 1000);
  const diff = dayDiffFromToday(d);
  if (diff === 0) return pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  if (diff === 1) return '昨天';
  if (d.getFullYear() === new Date().getFullYear()) return pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
  return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
}

/** 日期分隔：今天 / 昨天 / 5月1日 / 2024年5月1日 */
function dayLabel(seconds) {
  const d = new Date(seconds * 1000);
  const diff = dayDiffFromToday(d);
  if (diff === 0) return '今天';
  if (diff === 1) return '昨天';
  if (d.getFullYear() === new Date().getFullYear()) return (d.getMonth() + 1) + '月' + d.getDate() + '日';
  return d.getFullYear() + '年' + (d.getMonth() + 1) + '月' + d.getDate() + '日';
}

function clockLabel(seconds) {
  const d = new Date(seconds * 1000);
  return pad2(d.getHours()) + ':' + pad2(d.getMinutes());
}

function fullTime(seconds) {
  const d = new Date(seconds * 1000);
  return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' +
    pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
}

/** 名称首字取稳定的头像底色 */
function colorFor(seed) {
  const text = hasText(seed) ? String(seed) : '?';
  let hash = 0;
  for (let i = 0; i < text.length; i++) hash = (hash * 31 + text.charCodeAt(i)) % 360;
  return 'hsl(' + hash + ', 52%, 44%)';
}

/** 头像：有图用图（失败回落首字圆），无图直接首字彩色圆 */
function buildAvatar(name, avatarUrl, extraClass) {
  const label = hasText(name) ? String(name) : '?';
  const node = h('div', 'avatar' + (extraClass ? ' ' + extraClass : ''));
  const url = safeUrl(avatarUrl);
  if (url) {
    const img = document.createElement('img');
    img.alt = '';
    img.loading = 'lazy';
    img.referrerPolicy = 'no-referrer';
    img.addEventListener('error', () => applyInitial(node, label));
    img.src = url;
    node.appendChild(img);
  } else {
    applyInitial(node, label);
  }
  return node;
}

function applyInitial(node, label) {
  node.textContent = label.charAt(0);
  node.style.background = colorFor(label);
  node.title = label;
}

/** 会话列表里的一句话摘要（图片 / 语音等给出文字占位） */
function previewOf(msg) {
  if (!msg) return '';
  if (hasText(msg.text)) return String(msg.text);
  return segmentsText(msg.segments);
}

/** 把消息段压成一行文字（引用摘要、会话列表预览都用它） */
function segmentsText(segments) {
  const segs = Array.isArray(segments) ? segments : [];
  const parts = [];
  for (let i = 0; i < segs.length; i++) {
    const seg = segs[i];
    if (!seg || typeof seg !== 'object') continue;
    if (seg.type === 'text') { if (hasText(seg.text)) parts.push(String(seg.text)); continue; }
    if (seg.type === 'image') { parts.push('[图片]'); continue; }
    if (seg.type === 'voice') { parts.push('[语音]'); continue; }
    if (seg.type === 'video') { parts.push('[视频]'); continue; }
    if (seg.type === 'file') { parts.push('[文件]'); continue; }
    if (seg.type === 'face') { parts.push('[表情]'); continue; }
    if (seg.type === 'json' || seg.type === 'xml') { parts.push('[卡片消息]'); continue; }
    if (seg.type === 'forward') { parts.push('[合并转发]'); continue; }
    if (seg.type === 'reply') { parts.push('[回复]'); continue; }
    if (seg.type === 'at') {
      parts.push('@' + (hasText(seg.name) ? seg.name : (hasText(seg.target) ? seg.target : '某人')));
      continue;
    }
  }
  const text = parts.join('').replace(/\s+/g, ' ').trim();
  return text || '[消息]';
}

/** 引用消息里被引用内容的摘要文本 */
function quoteText(seg) {
  if (seg && hasText(seg.preview)) return String(seg.preview);
  if (seg && hasText(seg.text)) return String(seg.text);
  return '';
}

function shorten(text, max) {
  const value = hasText(text) ? String(text).replace(/\s+/g, ' ').trim() : '';
  const limit = max || 36;
  return value.length > limit ? value.slice(0, limit) + '…' : value;
}

/** 事件目标向上查找最近的匹配元素 */
function closestFrom(target, selector) {
  if (!target || typeof target.closest !== 'function') return null;
  return target.closest(selector);
}

/* --- 轻提示条 --- */
let toastTimer = null;
function toast(text, isError) {
  const box = el.toast;
  box.textContent = text || '';
  box.className = 'toast show' + (isError ? ' error' : '');
  box.hidden = false;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    box.className = 'toast' + (isError ? ' error' : '');
    setTimeout(() => { if (box.className.indexOf('show') < 0) box.hidden = true; }, 220);
  }, 2000);
}

/* --- 剪贴板（含离线兜底） --- */
function copyText(value) {
  const text = value === null || value === undefined ? '' : String(value);
  if (!text) { toast('没有可复制的内容'); return; }
  const done = () => toast('已复制');
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, () => legacyCopy(text, done));
  } else {
    legacyCopy(text, done);
  }
}

function legacyCopy(text, done) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', 'readonly');
  ta.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0';
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch (err) { ok = false; }
  document.body.removeChild(ta);
  if (ok) done(); else toast('复制失败，请手动选择文本', true);
}

/* ============================ 3. 接口请求层 ============================ */

/**
 * 统一请求封装：
 *  - 每个请求都带 X-BotUI-Token
 *  - 非 2xx 时解析 {"ok":false,"error":"..."} 并抛出可读错误
 *  - 401 统一弹出令牌页
 */
async function api(path, options) {
  const opts = options || {};
  const headers = { 'X-BotUI-Token': state.token || '' };
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json';

  let res;
  try {
    res = await fetch(API_BASE + path, {
      method: opts.method || 'GET',
      headers: headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      cache: 'no-store',
      credentials: 'same-origin'
    });
  } catch (err) {
    const netErr = new Error('无法连接服务器');
    netErr.status = 0;
    throw netErr;
  }

  const raw = await res.text().catch(() => '');
  let data = null;
  if (raw) {
    try { data = JSON.parse(raw); } catch (err) { data = null; }
  }
  if (!res.ok && !(data && typeof data === 'object')) data = null;

  if (!res.ok) {
    if (res.status === 401) requireToken();
    const message = (data && hasText(data.error)) ? String(data.error) : ('请求失败（HTTP ' + res.status + '）');
    const httpErr = new Error(message);
    httpErr.status = res.status;
    throw httpErr;
  }
  return data === null ? {} : data;
}

/* ============================ 4. 启动流程 ============================ */

/** 读取 ?token=，写入 localStorage，并从地址栏抹掉 */
function captureToken() {
  let stored = readStoredToken();
  let provided = '';
  try {
    provided = new URLSearchParams(location.search || '').get('token') || '';
  } catch (err) { provided = ''; }

  if (provided) {
    stored = provided;
    try { localStorage.setItem(TOKEN_KEY, provided); } catch (err) { /* 隐私模式下可能失败 */ }
    try {
      const url = new URL(location.href);
      url.searchParams.delete('token');
      history.replaceState(history.state, '', url.pathname + url.search + url.hash);
    } catch (err) { /* 忽略 */ }
  }
  state.token = stored;
}

function readStoredToken() {
  try { return localStorage.getItem(TOKEN_KEY) || ''; } catch (err) { return ''; }
}

function saveToken(value) {
  state.token = value;
  try { localStorage.setItem(TOKEN_KEY, value); } catch (err) { /* 忽略 */ }
}

/* --- 主题：默认深色，浅色通过 html[data-theme] --- */
function applyTheme(theme) {
  const next = theme === 'light' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  try { localStorage.setItem(THEME_KEY, next); } catch (err) { /* 忽略 */ }
}

function initTheme() {
  let saved = '';
  try { saved = localStorage.getItem(THEME_KEY) || ''; } catch (err) { saved = ''; }
  // 默认深色，只有用户主动切换过才读取本地偏好
  document.documentElement.setAttribute('data-theme', saved === 'light' ? 'light' : 'dark');
}

function toggleTheme() {
  applyTheme(document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light');
}

/* --- 令牌页 --- */
function showGate(message, desc) {
  state.gateOpen = true;
  state.gateFrom401 = !!message;
  el.gate.hidden = false;
  el.gateDesc.textContent = desc || '该服务已开启访问令牌校验，请输入令牌后继续。';
  el.gateInput.value = state.token || '';
  const old = el.gateForm.querySelector('.gate-error');
  if (old) old.remove();
  if (message) el.gateForm.appendChild(h('p', 'gate-error', message));
  renderConnMeta();
  setTimeout(() => { try { el.gateInput.focus(); } catch (err) { /* 忽略 */ } }, 30);
}

function hideGate() {
  state.gateOpen = false;
  state.gateFrom401 = false;
  el.gate.hidden = true;
  renderConnMeta();
}

/** 令牌缺失 / 失效：断开连接并弹出令牌页（401 统一入口） */
function requireToken() {
  closeSocket();
  state.started = false;
  state.online = false;
  if (state.gateOpen && state.gateFrom401) return;
  state.token = '';
  try { localStorage.removeItem(TOKEN_KEY); } catch (err) { /* 忽略 */ }
  showGate('令牌无效或已过期', '请重新输入访问令牌后继续。');
}

/* --- 连接状态 --- */
function setStatus(mode, text) {
  const level = mode === 'ok' || mode === 'poll' || mode === 'err' ? mode : 'idle';
  el.connDot.className = 'dot dot-' + level;
  el.connText.textContent = text || '';
  el.connText.title = text || '';
}

function statusOk() {
  setStatus('ok', '已连接 · 实时推送');
}

function statusErr(message) {
  setStatus('err', message || '连接异常');
  probeHealth();
}

/** 连接出错后探测 /health：数据库或机器人异常时在页脚说明原因 */
let probingHealth = false;
async function probeHealth() {
  if (probingHealth || state.gateOpen) return;
  probingHealth = true;
  try {
    const data = await api('/health');
    const notes = [];
    if (data && data.db === false) notes.push('数据库异常');
    const bots = data && Array.isArray(data.bots) ? data.bots.filter((b) => hasText(b)) : [];
    if (data && Array.isArray(data.bots) && !bots.length) notes.push('暂无在线机器人');
    if (notes.length) setStatus('err', '服务异常：' + notes.join('、'));
  } catch (err) {
    // 探测本身失败无需额外提示，连接错误已经展示
  } finally {
    probingHealth = false;
  }
}

function renderConnMeta() {
  const meta = state.meta;
  if (!meta || state.gateOpen) { el.connMeta.textContent = ''; return; }
  const parts = [];
  if (hasText(meta.self_id)) parts.push('机器人 ' + meta.self_id);
  if (hasText(meta.adapter)) parts.push(String(meta.adapter));
  if (hasText(meta.version)) parts.push('v' + meta.version);
  el.connMeta.textContent = parts.join(' · ');
}

/* --- 元信息 --- */
async function loadMeta() {
  state.meta = (await api('/meta')) || {};

  if (state.meta.auth_required && !state.token) {
    showGate('', '该服务已开启访问令牌校验，请输入令牌后继续。');
    return false;
  }
  if (state.gateOpen && !state.gateFrom401) hideGate();
  applyMeta();
  return true;
}

function applyMeta() {
  const meta = state.meta || {};
  const writable = meta.write_enabled !== false;

  el.input.disabled = !writable;
  el.sendBtn.disabled = !writable;
  el.atBtn.disabled = !writable;
  el.composer.classList.toggle('readonly', !writable);
  el.input.placeholder = writable ? '输入消息，Enter 发送，Shift+Enter 换行' : '只读模式，无法发送消息';
  el.hint.textContent = writable ? '' : '只读模式';

  // 分页大小也听后端的，别写死：后端 BOTUI_PAGE_SIZE 被限制在 10~200，
  // 前端若固定 50，改大改小都不会生效。
  const ps = Number(meta.page_size);
  state.pageSize = Number.isFinite(ps) && ps > 0 ? Math.floor(ps) : PAGE_LIMIT_FALLBACK;

  document.title = (hasText(meta.name) ? String(meta.name) : 'BotUI') + ' · 消息控制台';
  renderConnMeta();
  if (!state.gateOpen) statusOk();
  if (state.chats.length) renderChats();
}

/* ============================ 5. 会话列表 ============================ */

function sortChats() {
  state.chats.sort((a, b) => (parseTime(b.last_at) || 0) - (parseTime(a.last_at) || 0));
}

async function loadChats() {
  renderSkeletons();
  let data;
  try {
    data = await api('/chats?limit=200');
  } catch (err) {
    renderChatError(err);
    throw err;
  }

  const list = data && Array.isArray(data.chats) ? data.chats : [];
  state.chatMap.clear();
  state.chats = list.filter((c) => c && hasText(c.key)).map((raw) => {
    const chat = Object.assign({}, raw);
    chat.key = String(chat.key);
    if (!hasText(chat.name)) chat.name = hasText(chat.id) ? String(chat.id) : '未知会话';
    if (!state.unread.has(chat.key) && Number(chat.unread) > 0) {
      state.unread.set(chat.key, Math.floor(Number(chat.unread)));
    }
    state.chatMap.set(chat.key, chat);
    return chat;
  });
  if (state.current) state.current = state.chatMap.get(state.current.key) || state.current;
  sortChats();
  renderChats();
  renderHead();
}

function renderSkeletons() {
  const box = el.chatList;
  box.textContent = '';
  for (let i = 0; i < 5; i++) {
    const row = h('div', 'skeleton-item');
    row.appendChild(h('div', 'sk-box'));
    const lines = h('div', 'sk-lines');
    lines.appendChild(h('div', 'sk-line w60'));
    lines.appendChild(h('div', 'sk-line w85'));
    row.appendChild(lines);
    box.appendChild(row);
  }
}

function renderChatError(err) {
  const box = el.chatList;
  box.textContent = '';
  box.appendChild(emptyState('会话加载失败', err && err.message ? err.message : '未知错误'));
}

function renderChats() {
  const box = el.chatList;
  box.textContent = '';

  const keyword = state.search.trim().toLowerCase();
  const searching = state.searchMode === 'msgs' && !!keyword;

  // 消息搜索模式下由 renderSearchHits 负责结果区，这里只管会话列表
  el.searchTabs.hidden = !keyword;
  el.searchClear.hidden = !keyword;
  el.msgResults.hidden = !searching;
  box.hidden = searching;

  if (searching) {
    renderSearchHits();
    return;
  }

  if (!state.chats.length) {
    box.appendChild(emptyState('还没有任何会话', keyword ? '试试其他关键词' : '等待机器人收到消息'));
    return;
  }

  // 会话名 / 群号 / 最后一条消息都是本地过滤，即时响应
  const list = keyword ? state.chats.filter((c) => {
    return String(c.name || '').toLowerCase().indexOf(keyword) >= 0 ||
      String(c.id || '').toLowerCase().indexOf(keyword) >= 0 ||
      String(c.last_text || '').toLowerCase().indexOf(keyword) >= 0;
  }) : state.chats;

  if (!list.length) {
    box.appendChild(emptyState('没有匹配的会话', '试试切到「消息」搜索正文'));
    return;
  }

  const frag = document.createDocumentFragment();
  list.forEach((chat) => frag.appendChild(buildChatItem(chat)));
  box.appendChild(frag);
}

/** 把搜索关键词在文本里高亮出来（返回 DocumentFragment） */
function highlight(text, keyword) {
  const frag = document.createDocumentFragment();
  const source = String(text == null ? '' : text);
  const needle = String(keyword || '').toLowerCase();
  if (!needle) { frag.appendChild(document.createTextNode(source)); return frag; }

  const lower = source.toLowerCase();
  let from = 0;
  let at = lower.indexOf(needle);
  while (at >= 0) {
    if (at > from) frag.appendChild(document.createTextNode(source.slice(from, at)));
    frag.appendChild(h('mark', 'hit', source.slice(at, at + needle.length)));
    from = at + needle.length;
    at = lower.indexOf(needle, from);
  }
  frag.appendChild(document.createTextNode(source.slice(from)));
  return frag;
}

/** 关键词前后各截一段，避免长消息把结果撑爆 */
function snippet(text, keyword, span) {
  const source = String(text == null ? '' : text);
  const width = span || 60;
  if (source.length <= width * 2) return source;
  const at = source.toLowerCase().indexOf(String(keyword || '').toLowerCase());
  if (at < 0) return source.slice(0, width * 2) + '…';
  const start = Math.max(0, at - width);
  const end = Math.min(source.length, at + width);
  return (start > 0 ? '…' : '') + source.slice(start, end) + (end < source.length ? '…' : '');
}

function renderSearchHits() {
  const box = el.msgResults;
  box.textContent = '';
  const keyword = state.search.trim();

  if (state.searching && !state.searchHits.length) {
    for (let i = 0; i < 4; i++) {
      const row = h('div', 'skeleton-item');
      row.appendChild(h('div', 'sk-line w85'));
      box.appendChild(row);
    }
    return;
  }
  if (!state.searchHits.length) {
    box.appendChild(emptyState('没有找到匹配的消息', '换个关键词试试'));
    return;
  }

  const frag = document.createDocumentFragment();
  state.searchHits.forEach((msg) => {
    const item = h('div', 'hit-item');
    item.setAttribute('role', 'listitem');
    item.dataset.chatKey = msg.chat_key || '';
    item.dataset.msgId = String(msg.id);

    const top = h('div', 'hit-top');
    top.appendChild(h('span', 'hit-chat', msg.chat_name || msg.chat_id || '未知会话'));
    top.appendChild(h('span', 'hit-time', listTime(msg.ts)));
    item.appendChild(top);

    const who = h('span', 'hit-who');
    who.textContent = (msg.direction === 'out' ? '我' : (msg.user_name || msg.user_id || '未知')) + '：';
    const body = h('div', 'hit-text');
    body.appendChild(who);
    body.appendChild(highlight(snippet(msg.text, keyword), keyword));
    item.appendChild(body);

    frag.appendChild(item);
  });
  box.appendChild(frag);
}

/** 请求服务端做消息全文搜索 */
async function runSearch() {
  const keyword = state.search.trim();
  if (!keyword || state.searchMode !== 'msgs') { state.searchHits = []; return; }

  const seq = ++state.searchSeq;
  state.searching = true;
  renderSearchHits();
  try {
    const data = await api('/search?q=' + encodeURIComponent(keyword) + '&limit=100');
    // 期间又输入了新的关键词就丢弃这次结果
    if (seq !== state.searchSeq) return;
    state.searchHits = data && Array.isArray(data.messages) ? data.messages : [];
  } catch (err) {
    if (seq !== state.searchSeq) return;
    state.searchHits = [];
    toast('搜索失败：' + (err && err.message ? err.message : '未知错误'), true);
  } finally {
    if (seq === state.searchSeq) {
      state.searching = false;
      renderSearchHits();
    }
  }
}

function setSearchMode(mode) {
  if (state.searchMode === mode) return;
  state.searchMode = mode;
  const isMsgs = mode === 'msgs';
  el.tabChats.classList.toggle('active', !isMsgs);
  el.tabMsgs.classList.toggle('active', isMsgs);
  el.tabChats.setAttribute('aria-selected', String(!isMsgs));
  el.tabMsgs.setAttribute('aria-selected', String(isMsgs));
  renderChats();
  if (isMsgs && state.search.trim()) runSearch();
}

/** 点搜索结果：打开对应会话并定位到那条消息 */
async function openSearchHit(item) {
  const key = item.dataset.chatKey;
  const msgId = Number(item.dataset.msgId);
  if (!key) return;
  state.focusMsgId = Number.isFinite(msgId) ? msgId : null;
  await openChat(key);
}

function buildChatItem(chat) {
  const active = !!(state.current && state.current.key === chat.key);
  const item = h('div', 'chat-item' + (active ? ' active' : ''));
  item.setAttribute('role', 'listitem');
  item.dataset.key = chat.key;
  item.title = (chat.kind === 'group' ? '群聊' : '私聊') + ' · ' + String(chat.id || '');

  item.appendChild(buildAvatar(chat.name, chat.avatar));

  const main = h('div', 'chat-item-main');
  const top = h('div', 'chat-item-top');
  top.appendChild(h('div', 'chat-item-name', chat.name));

  const unread = state.unread.get(chat.key) || 0;
  if (unread > 0) {
    const pill = h('div', 'unread-pill', unread > 99 ? '99+' : String(unread));
    pill.title = unread + ' 条未读';
    top.appendChild(pill);
  }
  top.appendChild(h('div', 'chat-item-time', listTime(chat.last_at)));
  main.appendChild(top);

  const bottom = h('div', 'chat-item-bottom');
  const last = h('div', 'chat-item-last');
  const pending = state.pending.get(chat.key);
  if (pending) {
    // 未打开时有新消息：显示“新”标记 + 摘要
    last.appendChild(h('span', 'unread-pill muted', '新'));
    last.appendChild(document.createTextNode(' ' + (pending.text || '新消息')));
  } else {
    const text = hasText(chat.last_text) ? String(chat.last_text) : '[暂无内容]';
    last.textContent = chat.last_direction === 'out' ? '我: ' + text : text;
  }
  bottom.appendChild(last);
  main.appendChild(bottom);
  item.appendChild(main);
  return item;
}

function emptyState(title, sub) {
  const box = h('div', 'empty');
  const ico = h('div', 'empty-ico');
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
  circle.setAttribute('cx', '12');
  circle.setAttribute('cy', '12');
  circle.setAttribute('r', '7.6');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', 'M8.4 11.4h7.2M8.4 14.8h4.4');
  svg.appendChild(circle);
  svg.appendChild(path);
  ico.appendChild(svg);
  box.appendChild(ico);
  box.appendChild(h('div', null, title));
  if (hasText(sub)) box.appendChild(h('div', null, sub));
  return box;
}

/* --- 会话头部 --- */
function renderHead() {
  const chat = state.current;
  el.headAvatar.textContent = '';

  if (!chat) {
    const placeholder = h('div', 'avatar lg');
    applyInitial(placeholder, '?');
    el.headAvatar.appendChild(placeholder);
    el.headName.textContent = '未选择会话';
    el.headKind.hidden = true;
    el.headSub.textContent = '从左侧选择一个会话开始查看消息';
    return;
  }

  const isGroup = chat.kind === 'group';
  el.headAvatar.appendChild(buildAvatar(chat.name, chat.avatar, 'lg'));
  el.headName.textContent = chat.name;
  el.headKind.hidden = false;
  el.headKind.textContent = isGroup ? '群聊' : '私聊';
  el.headKind.className = 'badge' + (isGroup ? '' : ' kind-private');

  const parts = [];
  if (hasText(chat.id)) parts.push((isGroup ? '群号 ' : 'QQ ') + chat.id);
  const count = Number(chat.member_count);
  if (Number.isFinite(count) && count > 0) parts.push(count + ' 人');
  el.headSub.textContent = parts.length ? parts.join(' · ') : (isGroup ? '群聊' : '私聊');
}

/* ============================ 6. 消息渲染 ============================ */

/** 按时间插入日期 / 时间分隔符 */
function appendSeparators(container, seconds) {
  const t = parseTime(seconds);
  if (t === null) return;
  const label = dayLabel(t);
  if (label !== state.sep.day) {
    state.sep.day = label;
    state.sep.time = t;
    const sep = h('div', 'sep');
    sep.appendChild(h('span', null, label));
    container.appendChild(sep);
    return;
  }
  if (state.sep.time && t - state.sep.time > 300) {
    const sep = h('div', 'sep time');
    sep.appendChild(h('span', null, clockLabel(t)));
    container.appendChild(sep);
  }
  state.sep.time = t;
}

function buildMessage(msg) {
  const out = msg.direction === 'out';
  const isGroup = !!(state.current && state.current.kind === 'group');
  const prev = state.lastRendered;
  const sameUser = !!(prev && prev.user_id === msg.user_id && prev.direction === msg.direction);
  const compact = !out && isGroup && sameUser && !!prev;

  const row = h('div', 'msg ' + (out ? 'out' : 'in') + (compact ? ' compact' : ''));
  row.dataset.id = String(msg.id);

  // 入群消息显示头像；同一人连续发言时留出等宽占位
  if (!out) {
    const name = hasText(msg.user_name) ? String(msg.user_name) : String(msg.user_id || '未知用户');
    if (compact) row.appendChild(h('div', 'avatar-spacer'));
    else row.appendChild(buildAvatar(name, msg.user_avatar));
  }

  const body = h('div', 'msg-body');

  // 群聊里的入群消息展示昵称 + 身份 + 时间；私聊只在不紧凑时显示时间
  if (!out && (isGroup || !compact)) {
    const meta = h('div', 'msg-meta');
    meta.appendChild(h('span', 'msg-name', hasText(msg.user_name) ? String(msg.user_name) : String(msg.user_id || '未知用户')));
    if (msg.role === 'owner') meta.appendChild(h('span', 'role-tag owner', '群主'));
    else if (msg.role === 'admin') meta.appendChild(h('span', 'role-tag admin', '管理员'));
    const t = parseTime(msg.time);
    if (t !== null) {
      const time = h('span', 'msg-time', clockLabel(t));
      time.title = fullTime(t);
      meta.appendChild(time);
    }
    body.appendChild(meta);
  }

  const segs = Array.isArray(msg.segments) ? msg.segments : [];
  const segments = segs.length ? segs : (hasText(msg.text) ? [{ type: 'text', text: msg.text }] : []);
  body.appendChild(buildBubble(segments));
  row.appendChild(body);

  state.lastRendered = msg;
  return row;
}

function buildBubble(segments) {
  const hasImage = segments.some((s) => s && s.type === 'image' && safeUrl(s.url));
  const bubble = h('div', 'bubble' + (hasImage ? ' media' : ''));
  const hasContent = segments.some((s) => !(s && s.type === 'text' && !hasText(s.text)));

  if (!hasContent) {
    bubble.appendChild(h('span', 'seg-text', '[空消息]'));
    return bubble;
  }

  segments.forEach((seg) => {
    if (!seg || typeof seg !== 'object') return;
    switch (seg.type) {
      case 'text': {
        if (!hasText(seg.text)) return;
        const span = h('span', 'seg-text');
        span.textContent = String(seg.text); // 纯文本插入，保留换行
        bubble.appendChild(span);
        return;
      }
      case 'at': {
        const name = hasText(seg.name) ? String(seg.name) : (hasText(seg.target) ? String(seg.target) : '某人');
        const at = h('span', 'seg-at', '@' + name);
        if (hasText(seg.target)) at.title = 'QQ ' + seg.target;
        bubble.appendChild(at);
        return;
      }
      case 'image':
        bubble.appendChild(buildImage(seg));
        return;
      case 'face':
        bubble.appendChild(h('span', 'chip seg-face', '[表情 ' + (hasText(seg.id) ? seg.id : '?') + ']'));
        return;
      case 'reply': {
        bubble.appendChild(buildQuote(seg));
        return;
      }
      case 'voice':
        bubble.appendChild(placeholderChip('语音', seg.url));
        return;
      case 'audio':
        bubble.appendChild(placeholderChip('音频', seg.url));
        return;
      case 'video':
        bubble.appendChild(placeholderChip('视频', seg.url));
        return;
      case 'button': {
        const label = hasText(seg.label) ? String(seg.label) : '[按钮]';
        const chip = h('span', 'chip seg-button', label);
        if (hasText(seg.flag)) chip.title = '指令：' + seg.flag;
        const link = safeUrl(seg.url);
        if (link) {
          chip.appendChild(externalLink(link, '打开'));
          chip.classList.add('clickable');
        }
        bubble.appendChild(chip);
        return;
      }
      case 'file': {
        const chip = h('span', 'chip');
        chip.appendChild(h('span', null, '[文件] ' + (hasText(seg.name) ? String(seg.name) : '未命名文件')));
        const link = safeUrl(seg.url);
        if (link) {
          chip.appendChild(externalLink(link, '下载'));
          chip.classList.add('clickable');
        }
        bubble.appendChild(chip);
        return;
      }
      case 'json': {
        const chip = h('span', 'chip clickable', '[卡片消息]');
        chip.title = '点击复制原始数据';
        chip.dataset.copy = hasText(seg.data) ? String(seg.data) : '[卡片消息]';
        bubble.appendChild(chip);
        return;
      }
      default:
        bubble.appendChild(h('span', 'chip seg-unknown', '[不支持的段]'));
    }
  });
  return bubble;
}

/**
 * 引用消息。
 *
 * 只显示「回复 12345」是看不出回复了什么的，所以这里尽量渲染成一条引用块：
 * 有「谁 + 内容摘要」就显示它们，都没有就退化成原来的「回复 <id>」小标签，
 * 两种情况点击时都会跳到被引用的那条消息。
 */
function buildQuote(seg) {
  const id = hasText(seg.id) ? String(seg.id) : '';
  const who = hasText(seg.name) ? String(seg.name) : '';
  const body = quoteText(seg);

  if (!who && !body) {
    const chip = h('span', 'chip seg-reply clickable', '回复 ' + (id || '?'));
    if (id) {
      chip.dataset.replyId = id;
      chip.title = '点击定位被回复的消息';
    }
    return chip;
  }

  const quote = h('span', 'quote' + (id ? ' clickable' : ''));
  if (who) quote.appendChild(h('span', 'quote-who', who + '：'));
  quote.appendChild(h('span', 'quote-body', body || id || ''));
  if (id) {
    quote.dataset.replyId = id;
    quote.title = '消息 ' + id + ' · 点击定位被回复的消息';
  }
  return quote;
}

function placeholderChip(label, url) {
  const link = safeUrl(url);
  const chip = h('span', 'chip');
  chip.appendChild(h('span', null, '[' + label + ']'));
  if (link) {
    chip.appendChild(externalLink(link, '打开'));
    chip.classList.add('clickable');
  } else {
    chip.appendChild(h('span', null, '暂不支持播放'));
  }
  return chip;
}

function externalLink(href, text) {
  const a = h('a', 'chip-link', text);
  a.href = href;
  a.target = '_blank';
  a.rel = 'noopener noreferrer';
  return a;
}

function buildImage(seg) {
  const wrap = h('span', 'seg-block');
  const url = safeUrl(seg.url);
  if (!url) {
    wrap.appendChild(h('span', 'chip seg-unknown', '[图片] ' + (hasText(seg.file) ? String(seg.file) : '无法加载')));
    return wrap;
  }
  const img = document.createElement('img');
  img.className = 'seg-image';
  img.loading = 'lazy';
  img.decoding = 'async';
  img.referrerPolicy = 'no-referrer';
  img.alt = hasText(seg.name) ? String(seg.name) : (hasText(seg.file) ? String(seg.file) : '图片');
  img.src = url;
  img.addEventListener('error', () => {
    if (img.parentNode) img.parentNode.replaceChild(h('span', 'img-fallback', '图片加载失败'), img);
  });
  img.addEventListener('click', (ev) => {
    ev.stopPropagation();
    openLightbox(url);
  });
  wrap.appendChild(img);
  return wrap;
}

/** 会话切换时的骨架占位 */
function renderMessageSkeleton() {
  const box = el.msgList;
  box.textContent = '';
  for (let i = 0; i < 5; i++) {
    const row = h('div', 'msg ' + (i % 3 === 2 ? 'out' : 'in'));
    row.appendChild(h('div', 'avatar sm sk-box'));
    const body = h('div', 'msg-body');
    const bubble = h('div', 'bubble');
    const line = h('div', 'sk-line');
    line.style.width = (110 + (i * 47) % 150) + 'px';
    line.style.height = '12px';
    line.style.margin = '4px 0';
    bubble.appendChild(line);
    body.appendChild(bubble);
    row.appendChild(body);
    box.appendChild(row);
  }
}

/**
 * 重绘消息区。
 * reset=true 时整体重建（需要重算分隔符）；keepAnchor=true 时保持视觉位置（加载更早消息）。
 */
function renderMessages(reset, keepAnchor) {
  const box = el.msgList;
  const prevHeight = el.msgBox.scrollHeight;
  const prevTop = el.msgBox.scrollTop;
  const atBottom = isNearBottom();

  if (reset) {
    box.textContent = '';
    state.sep = { day: '', time: 0 };
    state.lastRendered = null;
  }

  const frag = document.createDocumentFragment();
  if (state.hasMore) {
    const more = h('div', 'load-more');
    const btn = h('button', null, state.loadingMore ? '加载中…' : '加载更早的消息');
    btn.type = 'button';
    btn.id = 'loadMoreBtn';
    btn.disabled = !!state.loadingMore;
    more.appendChild(btn);
    frag.appendChild(more);
  }
  if (!state.messages.length) {
    frag.appendChild(emptyState('暂无消息', '这个会话还没有任何消息记录'));
  } else {
    state.messages.forEach((msg) => {
      appendSeparators(frag, msg.time);
      frag.appendChild(buildMessage(msg));
    });
  }
  box.appendChild(frag);

  if (state.needsScroll) {
    state.needsScroll = false;
    scrollToBottom(false);
  } else if (keepAnchor || !atBottom) {
    // 不在底部时一定要补回滚动位置：重绘会把容器清空，滚动位置被顶到 0，
    // 不补回来就会「明明在看历史，来了一条新消息画面突然跳到别处」。
    const delta = el.msgBox.scrollHeight - prevHeight;
    el.msgBox.scrollTop = Math.max(0, prevTop + delta);
  } else {
    scrollToBottom(false);
  }
  updateNewMsgButton();
}

/** 追加单条消息（实时事件 / 发送成功） */
function appendMessage(msg) {
  if (!state.current || String(msg.chat) !== state.current.key) return false;
  const id = String(msg.id);
  if (state.msgMap.has(id)) return false;

  const atBottom = isNearBottom();
  state.messages.push(msg);
  state.msgMap.set(id, msg);

  if (state.messages.length > MSG_MAX) {
    const dropped = state.messages.shift();
    if (dropped) state.msgMap.delete(String(dropped.id));
    // 这里必须整段重绘（顶部少了一条，分隔符也要重算），
    // 而 renderMessages 内部已经处理了「不在底部就保持位置」。
    renderMessages(true);
    return true;
  }

  const empty = el.msgList.querySelector('.empty');
  if (empty) empty.remove();

  const frag = document.createDocumentFragment();
  appendSeparators(frag, msg.time);
  frag.appendChild(buildMessage(msg));
  el.msgList.appendChild(frag);
  // 只在本来就贴着底部时跟随；否则纹丝不动，让用户安心看历史
  if (atBottom) scrollToBottom(true);
  updateNewMsgButton();
  return true;
}

/* ============================ 7. 打开会话 / 历史 / 发送 ============================ */

async function openChat(key) {
  const chat = state.chatMap.get(String(key));
  if (!chat) return;

  state.current = chat;
  state.unread.set(chat.key, 0);
  state.pending.delete(chat.key);
  state.messages = [];
  state.msgMap.clear();
  state.hasMore = true;
  state.needsScroll = true;
  state.newCount = 0;
  state.replyTo = null;
  state.pendingAt = [];
  state.members = [];
  state.memberMap.clear();
  closeAtMenu();

  el.app.dataset.pane = 'chat';
  renderChats();
  renderHead();
  renderReplyBar();
  renderPendingAt();
  renderMessageSkeleton();
  updateNewMsgButton();

  try {
    // 用局部变量记住本次请求的条数，避免请求途中 pageSize 变化导致比较错位
    const limit = state.pageSize;
    const data = await api('/messages?chat=' + encodeURIComponent(chat.key) + '&limit=' + limit);
    const list = data && Array.isArray(data.messages) ? data.messages : [];
    if (!state.current || state.current.key !== chat.key) return; // 期间切换了会话
    state.messages = list.slice();
    state.messages.forEach((m) => state.msgMap.set(String(m.id), m));
    state.hasMore = list.length >= limit;
    renderMessages(true);
    markRead();
    // 打开会话算一次“刚同步过”：期间即使有事件漏掉，也不会马上触发重同步
    state.resyncLast = Date.now();
    // 从搜索结果进来时，等这一页渲染完再定位到那条消息
    if (state.focusMsgId != null) {
      const target = state.focusMsgId;
      state.focusMsgId = null;
      jumpToMessage(target);
    }
  } catch (err) {
    // 定位目标是一次性的：加载失败就丢掉，否则会在之后某次刷新时莫名其妙跳走
    state.focusMsgId = null;
    if (!state.current || state.current.key !== chat.key) return;
    if (err && err.status === 401) return; // 401 已由 api() 弹出令牌页
    el.msgList.textContent = '';
    el.msgList.appendChild(emptyState('消息加载失败', err && err.message ? err.message : '未知错误'));
    statusErr(err && err.message ? err.message : '消息加载失败');
  }
}

/** 向上滚动时加载更早的历史 */
async function loadOlder() {
  const chat = state.current;
  if (!chat || state.loadingMore || !state.hasMore) return;
  const oldest = state.messages.length ? parseTime(state.messages[0].time) : null;
  if (oldest === null) { state.hasMore = false; renderMessages(true); return; }
  // 同时带上最旧一条的行号：同一时刻可能有多条消息，只按时间戳翻页会漏掉它们
  const oldestId = state.messages.length ? String(state.messages[0].id) : '';

  state.loadingMore = true;
  renderMessages(true, true);
  try {
    const limit = state.pageSize;
    let url = '/messages?chat=' + encodeURIComponent(chat.key) +
      '&limit=' + limit + '&before=' + encodeURIComponent(String(oldest));
    if (oldestId) url += '&before_id=' + encodeURIComponent(oldestId);
    const data = await api(url);
    const list = data && Array.isArray(data.messages) ? data.messages : [];
    if (!state.current || state.current.key !== chat.key) return;

    const known = new Set(state.messages.map((m) => String(m.id)));
    const fresh = list.filter((m) => !known.has(String(m.id)));
    if (fresh.length) {
      state.messages = fresh.concat(state.messages);
      fresh.forEach((m) => state.msgMap.set(String(m.id), m));
      if (state.messages.length > MSG_MAX) {
        state.messages.slice(MSG_MAX).forEach((m) => state.msgMap.delete(String(m.id)));
        state.messages = state.messages.slice(0, MSG_MAX);
      }
    }
    // 返回不足一页 / 有重复 / 已达上限 → 说明没有更早的消息了
    if (!list.length || list.length < limit || fresh.length < list.length) state.hasMore = false;
    if (state.messages.length >= MSG_MAX) state.hasMore = false;
    const newOldest = state.messages.length ? parseTime(state.messages[0].time) : null;
    if (newOldest === null || newOldest >= oldest) state.hasMore = false;
  } catch (err) {
    if (!err || err.status !== 401) toast(err && err.message ? err.message : '加载更早的消息失败', true);
  } finally {
    state.loadingMore = false;
    if (state.current && state.current.key === chat.key) renderMessages(true, true);
  }
}

async function reloadCurrent() {
  const chat = state.current;
  if (!chat) return;
  setStatus('poll', '正在刷新…');
  state.current = null;
  await openChat(chat.key);
  if (state.started) statusOk();
}

async function sendMessage() {
  const chat = state.current;
  if (!chat || state.sending) return;
  if (state.meta && state.meta.write_enabled === false) { toast('只读模式，无法发送消息', true); return; }

  const text = el.input.value.replace(/\s+$/, '');
  const at = state.pendingAt.slice();
  if (!text && !at.length) return;

  const reply = state.replyTo;
  const body = { chat: chat.key, text: text };
  if (at.length) body.at = at;
  if (reply && hasText(reply.message_id)) body.reply_to = String(reply.message_id);

  state.sending = true;
  el.sendBtn.disabled = true;
  el.sendBtn.textContent = '发送中';
  setStatus('poll', '正在发送…');
  try {
    const data = await api('/send', { method: 'POST', body: body });
    const msg = data && data.message ? data.message : null;
    el.input.value = '';
    autoGrow();
    state.replyTo = null;
    state.pendingAt = [];
    renderReplyBar();
    renderPendingAt();

    if (msg && hasText(msg.chat) && String(msg.chat) === chat.key) {
      appendMessage(msg);
      updateChatList(msg, true);
    } else {
      await openChat(chat.key); // 未回传消息体：重新拉取
      updateChatList({ chat: chat.key, direction: 'out', text: text, time: Date.now() / 1000 }, true);
    }
  } catch (err) {
    if (!err || err.status !== 401) toast(err && err.message ? err.message : '发送失败', true);
  } finally {
    state.sending = false;
    el.sendBtn.disabled = !!(state.meta && state.meta.write_enabled === false);
    el.sendBtn.textContent = '发送';
    if (state.started && state.online) statusOk();
    el.input.focus();
  }
}

/** 收到 / 发出消息后更新会话列表（置顶、摘要、未读） */
function updateChatList(msg, isOpen) {
  if (!msg || !hasText(msg.chat)) return;
  const key = String(msg.chat);
  const chat = state.chatMap.get(key);
  if (!chat) { refreshChatsSoon(); return; }

  const preview = previewOf(msg);
  if (hasText(preview)) chat.last_text = preview;
  chat.last_direction = msg.direction === 'out' ? 'out' : 'in';
  const t = parseTime(msg.time);
  if (t !== null) chat.last_at = t;
  chat.message_count = (Number(chat.message_count) || 0) + 1;

  if (!isOpen) {
    state.unread.set(key, (state.unread.get(key) || 0) + 1);
    state.pending.set(key, { text: shorten(preview, 30) });
  }
  sortChats();
  renderChats();
}

let refreshTimer = null;
function refreshChatsSoon() {
  if (refreshTimer) return;
  refreshTimer = setTimeout(() => {
    refreshTimer = null;
    loadChats().catch(() => { /* 静默失败，等下一次重同步 */ });
  }, 400);
}

/* ============================ 8. 回复 / @ / 输入框 ============================ */

function renderReplyBar() {
  const msg = state.replyTo;
  if (!msg) {
    el.replyBar.hidden = true;
    el.replyBarLabel.textContent = '';
    return;
  }
  const who = hasText(msg.user_name) ? String(msg.user_name) : (hasText(msg.user_id) ? String(msg.user_id) : '');
  const id = hasText(msg.message_id) ? msg.message_id : msg.id;
  const preview = hasText(msg.text) ? shorten(msg.text, 24) : previewOf(msg);
  el.replyBarLabel.textContent = '回复 ' + id + (who ? ' · ' + who : '') + '：' + preview;
  el.replyBar.hidden = false;
}

function setReply(msg) {
  state.replyTo = msg || null;
  renderReplyBar();
  if (msg) el.input.focus();
}

function renderPendingAt() {
  const box = el.pendingAt;
  box.textContent = '';
  if (!state.pendingAt.length) { box.hidden = true; return; }
  box.hidden = false;
  state.pendingAt.forEach((target) => {
    const chip = h('span', 'at-chip');
    chip.appendChild(h('span', null, '@' + atName(target)));
    const remove = h('button', null, '✕');
    remove.type = 'button';
    remove.title = '移除该 @';
    remove.dataset.removeAt = target;
    chip.appendChild(remove);
    box.appendChild(chip);
  });
}

function atName(id) {
  const value = String(id || '');
  if (value === 'all') return '全体成员';
  const known = state.memberMap.get(value);
  if (known && hasText(known.name)) return String(known.name);
  const chat = state.current;
  // 私聊里 @ 的就是对方本人，直接用会话名
  if (chat && chat.kind === 'private' && String(chat.id) === value && hasText(chat.name)) return String(chat.name);
  return value;
}

function addAt(id, name) {
  const value = String(id || '').trim();
  if (!value) return;
  if (name) state.memberMap.set(value, { id: value, name: String(name) });
  if (state.pendingAt.indexOf(value) < 0) state.pendingAt.push(value);
  renderPendingAt();
}

/* ---------- @ 成员选择菜单 ----------
   以前这里弹一个输入框让用户敲 QQ 号，既容易敲错也没人记得住号码。
   现在改成从「记录到过的成员」里点选：名册由后端从历史消息里攒出来，
   本地再按输入的关键词过滤。名册为空时如实说明，不退回让人手填号码。 */
function renderAtMenu() {
  const menu = el.atMenu;
  menu.textContent = '';

  const query = String(state.atQuery || '').trim().toLowerCase();
  let items = state.members;
  if (query) {
    items = items.filter((m) =>
      String(m.name || '').toLowerCase().indexOf(query) >= 0 ||
      String(m.id || '').toLowerCase().indexOf(query) >= 0);
  }
  // 「全体成员」只在群聊里给，且要匹配关键词
  const chat = state.current;
  const canAll = !!(chat && chat.kind === 'group');
  if (canAll && (!query || '全体成员'.indexOf(query) >= 0 || 'all'.indexOf(query) >= 0)) {
    menu.appendChild(buildAtItem({ id: 'all', name: '全体成员', role: null }, true));
  }
  items.forEach((member) => menu.appendChild(buildAtItem(member, false)));

  if (!menu.childElementCount) {
    const tip = state.members.length
      ? '没有匹配的成员'
      : '还没有记录到成员。等群里有人发言、或有人被 @ 之后，这里就会列出来。';
    menu.appendChild(h('div', 'at-menu-empty', tip));
  }
  positionAtMenu();
}

function buildAtItem(member, isAll) {
  const id = String(member.id || '');
  const name = hasText(member.name) ? String(member.name) : id;
  const btn = h('button', 'at-item');
  btn.type = 'button';
  btn.setAttribute('role', 'menuitem');

  btn.appendChild(buildAvatar(isAll ? '@' : name, member.avatar, 'sm'));
  const main = h('div', 'at-item-main');
  main.appendChild(h('div', 'at-item-name', name));
  if (!isAll && hasText(id) && id !== name) main.appendChild(h('div', 'at-item-id', 'QQ ' + id));
  btn.appendChild(main);
  if (member.role === 'owner') btn.appendChild(h('span', 'role-tag owner', '群主'));
  else if (member.role === 'admin') btn.appendChild(h('span', 'role-tag admin', '管理员'));

  btn.addEventListener('click', () => {
    addAt(id, name);
    closeAtMenu();
    el.input.focus();
  });
  return btn;
}

function positionAtMenu() {
  const menu = el.atMenu;
  const anchor = el.atBtn.getBoundingClientRect();
  menu.hidden = false;
  const rect = menu.getBoundingClientRect();
  const left = Math.max(6, Math.min(anchor.left, window.innerWidth - rect.width - 6));
  // 默认贴着按钮上沿往上弹（输入区在页面底部），放不下就往下摆
  let top = anchor.top - rect.height - 6;
  if (top < 6) top = Math.min(anchor.bottom + 6, window.innerHeight - rect.height - 6);
  menu.style.left = left + 'px';
  menu.style.top = Math.max(6, top) + 'px';
}

async function loadMembers() {
  const chat = state.current;
  if (!chat) return;
  const key = chat.key;
  try {
    const data = await api('/members?chat=' + encodeURIComponent(key) + '&limit=500');
    if (!state.current || state.current.key !== key) return;
    state.members = data && Array.isArray(data.members) ? data.members : [];
  } catch (err) {
    if (err && err.status === 401) return;
    state.members = [];
  }
  state.memberMap.clear();
  state.members.forEach((m) => {
    if (m && hasText(m.id)) state.memberMap.set(String(m.id), m);
  });
  // 名册可能补上了昵称，刷新一下已选 @ 的显示
  renderPendingAt();
  if (!el.atMenu.hidden) renderAtMenu();
}

async function openAtMenu() {
  const chat = state.current;
  if (!chat) { toast('请先选择一个会话'); return; }
  if (state.meta && state.meta.write_enabled === false) { toast('只读模式，无法提及成员', true); return; }
  state.atQuery = '';
  el.atMenu.textContent = '';
  el.atMenu.hidden = false;
  await loadMembers();
  if (el.atMenu.hidden) return;  // 加载期间被关掉了
  renderAtMenu();
  el.atMenu.focus();
}

function closeAtMenu() {
  if (el.atMenu.hidden) return;
  el.atMenu.hidden = true;
  el.atMenu.textContent = '';
  state.atQuery = '';
}

function toggleAtMenu() {
  if (el.atMenu.hidden) openAtMenu();
  else closeAtMenu();
}

function atMenuKeydown(ev) {
  if (ev.key === 'Escape') { ev.preventDefault(); closeAtMenu(); el.input.focus(); return; }
  if (ev.key === 'Enter') {
    const first = el.atMenu.querySelector('.at-item');
    if (first) { ev.preventDefault(); first.click(); }
    return;
  }
  if (ev.key === 'Backspace') {
    // 退格删掉搜索词的最后一个字符；删空后不关闭菜单
    ev.preventDefault();
    state.atQuery = String(state.atQuery || '').slice(0, -1);
    renderAtMenu();
    return;
  }
  if (ev.key && ev.key.length === 1 && !ev.ctrlKey && !ev.metaKey && !ev.altKey) {
    ev.preventDefault();
    state.atQuery = String(state.atQuery || '') + ev.key;
    renderAtMenu();
  }
}

/** 输入框随内容增高，最多约 5 行 */
function autoGrow() {
  const box = el.input;
  box.style.height = 'auto';
  box.style.height = Math.min(box.scrollHeight, 132) + 'px';
  box.style.overflowY = box.scrollHeight > 132 ? 'auto' : 'hidden';
}

/* ============================ 9. 灯箱与右键菜单 ============================ */

function openLightbox(url) {
  const link = safeUrl(url);
  if (!link) return;
  el.lightboxImg.src = link;
  el.lightbox.hidden = false;
}

function closeLightbox() {
  if (el.lightbox.hidden) return;
  el.lightbox.hidden = true;
  el.lightboxImg.removeAttribute('src');
}

function openCtxMenu(x, y, msg) {
  state.ctxMsg = msg;
  const menu = el.ctxMenu;
  menu.textContent = '';

  const capability = state.meta && state.meta.capabilities ? state.meta.capabilities.recall !== false : true;
  const recallable = msg.recallable === true && capability;

  const items = [
    { label: '复制文本', run: () => copyText(hasText(msg.text) ? msg.text : previewOf(msg)) },
    { label: '回复', run: () => setReply(msg) },
    { label: '撤回', run: () => recallMessage(msg), disabled: !recallable, danger: true },
    { sep: true },
    { label: '复制消息ID', run: () => copyText(hasText(msg.message_id) ? msg.message_id : msg.id) }
  ];

  items.forEach((item) => {
    if (item.sep) { menu.appendChild(h('div', 'ctx-sep')); return; }
    const btn = h('button', 'ctx-item' + (item.danger ? ' danger' : ''), item.label);
    btn.type = 'button';
    btn.setAttribute('role', 'menuitem');
    if (item.disabled) btn.disabled = true;
    btn.addEventListener('click', () => {
      closeCtxMenu();
      item.run();
    });
    menu.appendChild(btn);
  });

  menu.hidden = false;
  const rect = menu.getBoundingClientRect();
  menu.style.left = Math.max(6, Math.min(x, window.innerWidth - rect.width - 6)) + 'px';
  menu.style.top = Math.max(6, Math.min(y, window.innerHeight - rect.height - 6)) + 'px';
}

function closeCtxMenu() {
  if (el.ctxMenu.hidden) return;
  el.ctxMenu.hidden = true;
  el.ctxMenu.textContent = '';
  state.ctxMsg = null;
}

async function recallMessage(msg) {
  if (!msg) return;
  const chat = hasText(msg.chat) ? String(msg.chat) : (state.current ? state.current.key : '');
  if (!chat || !hasText(msg.id)) return;
  try {
    await api('/recall', { method: 'POST', body: { chat: chat, id: msg.id } });
    removeMessage(msg.id);
    toast('已撤回');
  } catch (err) {
    if (!err || err.status !== 401) toast(err && err.message ? err.message : '撤回失败', true);
  }
}

function removeMessage(id) {
  const key = String(id);
  const index = state.messages.findIndex((m) => String(m.id) === key);
  if (index < 0 && !state.msgMap.has(key)) return;

  if (index >= 0) state.messages.splice(index, 1);
  state.msgMap.delete(key);
  if (state.replyTo && String(state.replyTo.id) === key) setReply(null);

  // 删掉的是首条消息时会顺带带走日期分隔符，因此整体重绘
  if (index === 0) { renderMessages(true); return; }
  const node = el.msgList.querySelector('.msg[data-id="' + cssEscape(key) + '"]');
  if (node) node.remove();
  if (!state.messages.length) renderMessages(true);
}

function cssEscape(value) {
  if (window.CSS && typeof window.CSS.escape === 'function') return window.CSS.escape(String(value));
  return String(value).replace(/["\\]/g, '\\$&');
}

/* ============================ 10. 滚动与已读 ============================ */

function isNearBottom() {
  const box = el.msgBox;
  return box.scrollHeight - box.scrollTop - box.clientHeight < NEAR_BOTTOM;
}

function scrollToBottom(smooth) {
  const box = el.msgBox;
  if (smooth) {
    box.scrollTo({ top: box.scrollHeight, behavior: 'smooth' });
  } else {
    const saved = box.style.scrollBehavior;
    box.style.scrollBehavior = 'auto';
    box.scrollTop = box.scrollHeight;
    box.style.scrollBehavior = saved || '';
  }
  state.newCount = 0;
  updateNewMsgButton();
}

function updateNewMsgButton() {
  const btn = el.newMsgBtn;
  if (!state.newCount) { btn.hidden = true; return; }
  btn.hidden = false;
  btn.textContent = state.newCount + ' 条新消息';
}

function markRead() {
  if (!state.current) return;
  const key = state.current.key;
  // 没有变化就不重绘，避免滚动时反复重建列表
  if (!(state.unread.get(key) || 0) && !state.pending.has(key)) return;
  state.unread.set(key, 0);
  state.pending.delete(key);
  renderChats();
}

/* ============================ 11. 实时事件（WebSocket） ============================ */

function wsUrl(since) {
  const url = new URL(API_BASE + '/ws');
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.searchParams.set('since', String(since || 0));
  url.searchParams.set('token', state.token || '');
  return url.href;
}

function startRealtime() {
  closeSocket();
  state.since = 0;
  state.online = false;
  state.wsFails = 0;
  state.wsDelay = BACKOFF_MIN;
  state.resyncLast = 0;
  connectSocket();
}

function closeSocket() {
  if (state.wsTimer) {
    clearTimeout(state.wsTimer);
    state.wsTimer = null;
  }
  state.wsGen += 1; // 让旧连接的回调全部失效
  const ws = state.ws;
  state.ws = null;
  if (!ws) return;
  ws.onopen = null;
  ws.onmessage = null;
  ws.onerror = null;
  ws.onclose = null;
  try { ws.close(); } catch (err) { /* 忽略 */ }
}

function scheduleReconnect() {
  if (state.gateOpen || !state.started || state.wsTimer) return;
  state.wsTimer = setTimeout(() => {
    state.wsTimer = null;
    connectSocket();
  }, state.wsDelay);
  // 退避阶梯：1s → 2s → 4s → 8s → 16s → 30s，连上后重置
  state.wsDelay = Math.min(BACKOFF_MAX, BACKOFF_MIN * Math.pow(2, state.wsFails));
}

function connectSocket() {
  if (state.gateOpen || !state.started) return;
  closeSocket();
  const gen = state.wsGen;

  let ws;
  try {
    ws = new WebSocket(wsUrl(state.since));
  } catch (err) {
    state.wsFails += 1;
    statusErr('无法建立实时连接，正在重试…');
    scheduleReconnect();
    return;
  }
  state.ws = ws;

  ws.onopen = () => {
    if (gen !== state.wsGen) return;
    const reconnect = state.wsFails > 0;
    state.wsFails = 0;
    state.wsDelay = BACKOFF_MIN;
    state.resyncLast = Date.now();
    state.online = true;
    statusOk();
    // 重连回来时对齐一次列表与会话：断开期间漏掉的事件由服务端补发，
    // 但会话摘要这类「派生状态」还是以接口为准更划算。
    if (reconnect) resync(state.since, true);
  };

  ws.onmessage = (ev) => {
    if (gen !== state.wsGen) return;
    let payload = null;
    try { payload = JSON.parse(ev.data); } catch (err) { return; }
    if (!payload || typeof payload !== 'object') return;
    state.resyncLast = Date.now();
    handleEvent(payload);
  };

  ws.onerror = () => {
    if (gen !== state.wsGen) return;
    // 具体原因交给 onclose（那里能拿到 code），这里只保证状态提示不是「已连接」
    if (state.online) statusErr('实时连接异常');
  };

  ws.onclose = (ev) => {
    if (gen !== state.wsGen) return;
    state.ws = null;
    state.online = false;

    const code = ev && ev.code;
    if (code === 4401) { requireToken(); return; } // 令牌失效：api() 的 401 入口
    if (code === 4403) {
      setStatus('err', '该来源被拒绝访问（仅允许本机，见 BOTUI_ALLOW_REMOTE）');
      return; // 重连也没用，等用户处理
    }
    if (state.gateOpen || !state.started) return;

    state.wsFails += 1;
    statusErr('实时连接已断开，正在重连…');
    scheduleReconnect();
  };
}

/** 长连接长时间静默（sleep / 代理假死）后主动重同步一次 */
function checkStale() {
  if (!state.online || !state.resyncLast) return;
  if (Date.now() - state.resyncLast < RESYNC_STALE * 1000) return;
  resync(state.since, true);
}

/**
 * 重新同步：把会话列表与当前会话的消息重新拉一遍。
 * 事件只是「增量提示」，页面状态始终以这两个接口为准，所以无论因为什么原因
 * 怀疑自己落后了（序号空洞、长时间静默、重连回来），都可以靠它兜底。
 */
async function resync(since, silent) {
  if (state.syncing) return;
  state.syncing = true;
  state.resyncLast = Date.now();
  try {
    await loadChats();
    if (state.current) {
      const key = state.current.key;
      const limit = state.pageSize;
      const data = await api('/messages?chat=' + encodeURIComponent(key) + '&limit=' + limit);
      if (state.current && state.current.key === key) {
        const list = data && Array.isArray(data.messages) ? data.messages : [];
        applyMessageList(list, limit);
      }
    }
    state.since = Math.max(state.since, Number(since) || 0);
  } catch (err) {
    // 重同步失败不是致命问题：连接断开时下一次重连还会再试一次
    if (!silent && err && err.status !== 401) statusErr(err.message || '同步失败');
  } finally {
    state.syncing = false;
  }
}

/** 用接口返回的整页消息替换当前列表（保留滚动位置） */
function applyMessageList(list, limit) {
  const atBottom = isNearBottom();
  const prevHeight = el.msgBox.scrollHeight;
  const prevTop = el.msgBox.scrollTop;

  state.messages = list.slice();
  state.msgMap.clear();
  state.messages.forEach((m) => state.msgMap.set(String(m.id), m));
  state.hasMore = list.length >= limit;
  state.newCount = 0;
  renderMessages(true);

  if (!atBottom) el.msgBox.scrollTop = Math.max(0, prevTop + (el.msgBox.scrollHeight - prevHeight));
  updateNewMsgButton();
  markRead();
}

/** 处理单条事件，未知类型直接忽略 */
function handleEvent(ev) {
  if (!ev || typeof ev !== 'object') return;

  if (ev.type === 'ping' || ev.type === 'ready') {
    if (Number.isFinite(Number(ev.cursor))) state.since = Math.max(state.since, Number(ev.cursor));
    return;
  }

  // 序号出现空洞说明中间的事件丢了（服务端队列满、或者曾短暂断开），
  // 这时不做花哨的补洞，直接整体重同步一次最省心。
  const seq = Number(ev.seq);
  if (Number.isFinite(seq) && seq > 0) {
    if (state.since && seq > state.since + RESYNC_GAP) {
      resync(state.since, true);
      return;
    }
    if (seq > state.since) state.since = seq;
  }

  if (ev.type === 'message' && ev.message) {
    const msg = ev.message;
    const key = String(msg.chat);
    const isOpen = !!(state.current && state.current.key === key);

    if (isOpen) {
      const atBottom = isNearBottom();
      // 不在底部时不打扰阅读，只浮出“N 条新消息”
      if (appendMessage(msg) && !atBottom) {
        state.newCount += 1;
        updateNewMsgButton();
      }
    }
    if (ev.chat && typeof ev.chat === 'object' && hasText(ev.chat.key)) mergeChat(ev.chat);
    updateChatList(msg, isOpen);
    return;
  }

  if (ev.type === 'recall' && ev.id !== undefined && ev.id !== null) {
    removeMessage(ev.id);
  }
}

/** 合并事件携带的会话信息（新会话自动补入列表） */
function mergeChat(chat) {
  const key = String(chat.key);
  const existing = state.chatMap.get(key);
  if (existing) {
    if (hasText(chat.name)) existing.name = chat.name;
    if (chat.avatar !== undefined) existing.avatar = chat.avatar;
    if (hasText(chat.kind)) existing.kind = chat.kind;
    if (hasText(chat.id)) existing.id = chat.id;
    const count = Number(chat.member_count);
    if (Number.isFinite(count) && count > 0) existing.member_count = count;
    if (state.current && state.current.key === key) renderHead();
    return;
  }
  const item = Object.assign({}, chat);
  item.key = key;
  if (!hasText(item.name)) item.name = hasText(item.id) ? String(item.id) : '未知会话';
  state.chatMap.set(key, item);
  state.chats.push(item);
  sortChats();
  renderChats();
}

/* ============================ 12. 启动与事件绑定 ============================ */

async function enterApp() {
  state.started = true;
  state.online = false;
  await loadChats();
  startRealtime();
}

async function bootstrap() {
  initTheme();
  captureToken();
  renderConnMeta();

  try {
    const ready = await loadMeta();
    if (!ready) { renderChats(); renderHead(); return; }
    await enterApp();
  } catch (err) {
    if (err && err.status === 401) { renderChats(); renderHead(); return; }
    statusErr(err && err.message ? err.message : '初始化失败');
    renderChatError(err);
    // 初始化失败也保持连接重试，网络恢复后自动接上
    state.started = true;
    startRealtime();
  }
}

/** 保存令牌后重新进入应用 */
async function submitToken() {
  hideGate();
  closeSocket();
  state.started = false;
  state.since = 0;
  try {
    const ready = await loadMeta();
    if (!ready) return;
    await enterApp();
    if (state.current) openChat(state.current.key);
    toast('令牌已保存');
  } catch (err) {
    if (err && err.status === 401) return; // api() 已重新弹出令牌页
    statusErr(err && err.message ? err.message : '令牌校验失败');
    toast(err && err.message ? err.message : '令牌校验失败', true);
  }
}

function bindEvents() {
  el.themeBtn.addEventListener('click', toggleTheme);

  // 搜索：会话名本地即时过滤；「消息」标签下再请求服务端做正文检索
  let searchTimer = null;
  el.searchInput.addEventListener('input', () => {
    state.search = el.searchInput.value || '';
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      renderChats();
      if (state.searchMode === 'msgs' && state.search.trim()) runSearch();
      else { state.searchHits = []; }
    }, 120);
  });

  el.searchClear.addEventListener('click', () => {
    el.searchInput.value = '';
    state.search = '';
    state.searchHits = [];
    state.searchSeq += 1;   // 让在途请求的结果失效
    el.searchInput.focus();
    renderChats();
  });
  el.tabChats.addEventListener('click', () => setSearchMode('chats'));
  el.tabMsgs.addEventListener('click', () => setSearchMode('msgs'));
  el.msgResults.addEventListener('click', (ev) => {
    const item = closestFrom(ev.target, '.hit-item');
    if (item) openSearchHit(item);
  });

  // 会话列表：事件委托
  el.chatList.addEventListener('click', (ev) => {
    const item = closestFrom(ev.target, '.chat-item');
    if (item && item.dataset.key) openChat(item.dataset.key);
  });

  // 窄屏返回列表
  el.backBtn.addEventListener('click', () => { el.app.dataset.pane = 'sidebar'; });

  el.reloadBtn.addEventListener('click', () => { reloadCurrent(); });

  // 消息区：加载更早 / 定位被回复消息 / 复制卡片
  el.msgList.addEventListener('click', (ev) => {
    if (closestFrom(ev.target, '#loadMoreBtn')) { loadOlder(); return; }

    const replyChip = closestFrom(ev.target, '[data-reply-id]');
    if (replyChip) {
      jumpToMessage(replyChip.dataset.replyId);
      return;
    }
    const copyChip = closestFrom(ev.target, '[data-copy]');
    if (copyChip) copyText(copyChip.dataset.copy);
  });

  // 右键菜单
  el.msgList.addEventListener('contextmenu', (ev) => {
    const row = closestFrom(ev.target, '.msg');
    if (!row || !row.dataset.id) return;
    const msg = state.msgMap.get(String(row.dataset.id));
    if (!msg) return;
    ev.preventDefault();
    openCtxMenu(ev.clientX, ev.clientY, msg);
  });

  // 滚动到底部附近即视为已读
  el.msgBox.addEventListener('scroll', () => {
    if (!isNearBottom()) return;
    if (state.newCount) { state.newCount = 0; updateNewMsgButton(); }
    markRead();
  }, { passive: true });

  el.newMsgBtn.addEventListener('click', () => scrollToBottom(true));

  // 输入框：Enter 发送，Shift+Enter 换行
  el.input.addEventListener('input', autoGrow);
  el.input.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing) {
      ev.preventDefault();
      sendMessage();
    }
  });
  el.sendBtn.addEventListener('click', sendMessage);

  // 回复条 / @
  el.replyBarClose.addEventListener('click', () => setReply(null));
  el.atBtn.addEventListener('click', toggleAtMenu);
  el.atMenu.addEventListener('keydown', atMenuKeydown);
  el.pendingAt.addEventListener('click', (ev) => {
    const btn = closestFrom(ev.target, '[data-remove-at]');
    if (!btn) return;
    state.pendingAt = state.pendingAt.filter((item) => item !== btn.dataset.removeAt);
    renderPendingAt();
  });

  // 灯箱
  el.lightbox.addEventListener('click', closeLightbox);

  // 令牌表单
  el.gateForm.addEventListener('submit', (ev) => {
    ev.preventDefault();
    const value = (el.gateInput.value || '').trim();
    if (!value) { toast('请输入访问令牌', true); return; }
    saveToken(value);
    submitToken();
  });

  // Esc / 点击空白关闭浮层
  document.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Escape') return;
    if (!el.lightbox.hidden) { closeLightbox(); return; }
    if (!el.atMenu.hidden) { closeAtMenu(); return; }
    closeCtxMenu();
  });
  document.addEventListener('click', (ev) => {
    if (!el.atMenu.hidden && !el.atMenu.contains(ev.target) && ev.target !== el.atBtn) {
      closeAtMenu();
    }
    if (el.ctxMenu.hidden) return;
    if (!el.ctxMenu.contains(ev.target)) closeCtxMenu();
  });
  window.addEventListener('resize', () => { closeAtMenu(); closeCtxMenu(); });
  window.addEventListener('blur', closeCtxMenu);

  // 页面重新可见时补一次同步：后台标签页的定时器会被浏览器节流，
  // 长连接也可能已经被中间设备掐掉，回来先对齐再继续。
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible' || !state.started) return;
    if (!state.ws || state.ws.readyState === WebSocket.CLOSED) connectSocket();
    else resync(state.since, true);
  });
}

// 心跳与静默检测：前者是 WebSocket 协议层保活（挡掉代理空闲断连），
// 后者比对最近一次收到服务端消息的时间，发现假死就重新同步。
const WS_PING = 30000;
setInterval(() => {
  if (state.gateOpen || !state.started) return;
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    try { state.ws.send('ping'); } catch (err) { /* 交给 onclose 处理 */ }
  }
  checkStale();
}, WS_PING);

/** 定位到被回复的消息并高亮 */
function jumpToMessage(id) {
  const node = el.msgList.querySelector('.msg[data-id="' + cssEscape(id) + '"]');
  if (!node) {
    // 目标不在已加载的这一页（搜索结果可能很旧），把当前会话翻到最新再提示
    if (String(id) !== '') toast('这条消息不在已加载的记录里，可点「重新加载」或继续向上翻');
    return;
  }
  node.scrollIntoView({ block: 'center', behavior: 'smooth' });
  const bubble = node.querySelector('.bubble');
  if (!bubble) return;
  const saved = bubble.style.boxShadow;
  bubble.style.transition = 'box-shadow 0.2s ease';
  bubble.style.boxShadow = '0 0 0 2px var(--accent)';
  setTimeout(() => { bubble.style.boxShadow = saved; }, 1200);
}

// 入口
bindEvents();
bootstrap();
