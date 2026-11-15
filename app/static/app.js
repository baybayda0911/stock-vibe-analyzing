'use strict';

const pages = {overview: '研究工作台', company: '研究总览', fundamental: '基本面', technical: '技术面', research: '判断验证', comparison: '横向比较', archive: '档案室', tasks: '任务', settings: '设置'};
const companyPages = ['company', 'fundamental', 'technical', 'research'];
const moduleNames = {fundamental: '基本面', technical: '技术面', review: '研究回顾', broker_source: '补充研报', source_adaptation: '补充研究解读'};
const formalRoles = new Set(['full_research', 'incremental_research', 'correction']);
const sourceRoles = new Set(['broker_source', 'source_adaptation']);
const state = {bootstrap: null, securities: new Map(), watchlist: [], selected: null, contexts: new Map(), technical: new Map(), fundamental: new Map(), analyst: new Map(), continuity: new Map(), tasks: new Map(), tasksStatus: 'idle', tasksError: '', selectedTaskId: null, taskRenderKey: '', page: 'overview', generation: 0, selection: 0, saving: false, techPeriod: '1d', homePeriod: '1d', finPeriod: 'quarterly', indicator: 'macd', chartWindow: 60, chartStart: null, ma: new Set(['ma5']), draft: null, draftSecurity: null};
state.comparison = {pool: [], status: 'idle', data: null, error: ''};
state.localData = new Map();
let localDataReadVersion = 0;
let localDataPollTimer;
const localDataPending = new Set();
const localDataResultsSeen = new Set();
const localDataStatuses = {prepared: '范围已核对，等待你确认', queued: '等待重算', running: '正在重算', completed: '新快照已保存', partial: '部分成果已保存', failed: '更新未完成', interrupted: '重算已中断'};
let comparisonReadVersion = 0;
let comparisonController;
let comparisonRenderKey = '';
const comparisonColors = ['#006f6a', '#b18238', '#527caa', '#7d67a8', '#a65d74'];
const comparisonLabels = {comparable: '可比', diagnostic: '仅供诊断', not_comparable: '不可直接比较', unavailable: '资料不足'};
let announceTimer;
let searchTimer;
let searchRequest = 0;
let searchController;
let readerTrigger;
let readerTimer;
let draftTrigger;
let chartView;
let continuityReviewDraft = null;
let continuityReviewSaving = false;
const analystArchivePending = new Set();
const analystArchiveOutcomes = new Map();
let taskPollTimer;
let tasksReadVersion = 0;
let researchStarting = false;
let draftSubmission = null;
let draftSourceIds = [];
let draftItemIds = [];
const taskActionPending = new Set();
const taskAnswerDrafts = new Map();
const taskResultsSeen = new Set();
const taskStatuses = {queued: '等待开始', running: '正在研究', waiting_input: '等待你补充', cancelling: '正在停止', cancelled: '已取消', interrupted: '运行已中断', failed: '暂未完成', archiving: '正在校验归档', completed: '已完成', partial: '部分成果已保存'};
const taskModes = {full: '完整研究', continue: '延续研究', broker_update: '研报驱动更新'};
const periodNames = {'1d': '日线', '1w': '周线', '1mo': '月线', '6mo': '半年线', '1y': '年线'};
const maNames = {ma5: 'MA5', sma20: 'MA20', sma60: 'MA60', sma120: 'MA120'};
const maColors = {ma5: '#b18238', sma20: '#006f6a', sma60: '#7d67a8', sma120: '#527caa'};

const byId = id => document.getElementById(id);
const list = value => Array.isArray(value) ? value : [];
const text = value => value == null ? '' : String(value);
// shortcut: 偏好只覆盖本浏览器同一运行地址，需跨服务端口恢复时再采用私有后端存储。
const viewPreferencesKey = 'stock-research-view/1';
let viewPreferencesLoaded = false;
let lastViewPreferences = '';

function savedDay(value) {
  return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value && value <= new Date().toLocaleDateString('sv-SE', {timeZone: 'Asia/Shanghai'});
}

function saveViewPreferences() {
  if (!viewPreferencesLoaded || !state.bootstrap) return;
  const value = JSON.stringify({schema: 1, security_id: state.selected?.security_id || null, page: state.page,
    techPeriod: state.techPeriod, homePeriod: state.homePeriod, finPeriod: state.finPeriod, indicator: state.indicator,
    chartWindow: state.chartWindow, ma: [...state.ma], comparison: comparisonRequest()});
  if (value === lastViewPreferences) return;
  try { window.localStorage.setItem(viewPreferencesKey, value); lastViewPreferences = value; }
  catch { byId('view-preferences-note').textContent = '浏览器未允许保存偏好，本页仍可使用；刷新后重新选择。'; }
}

function restoreViewPreferences() {
  try {
    const raw = window.localStorage.getItem(viewPreferencesKey);
    if (!raw || raw.length > 12000) return null;
    const value = JSON.parse(raw);
    if (!value || value.schema !== 1) return null;
    if (Object.hasOwn(pages, value.page)) state.page = value.page;
    for (const [key, allowed] of Object.entries({techPeriod: Object.keys(periodNames), homePeriod: ['1d', '1w', '1mo', '1y'], finPeriod: ['quarterly', 'annual'], indicator: ['macd', 'rsi']})) {
      if (allowed.includes(value[key])) state[key] = value[key];
    }
    if (value.chartWindow === 'all' || Number.isInteger(value.chartWindow) && value.chartWindow >= 10 && value.chartWindow <= 2000) state.chartWindow = value.chartWindow;
    const ma = list(value.ma).filter(key => Object.hasOwn(maNames, key));
    const request = value.comparison;
    if (request && typeof request === 'object') {
      state.comparison.pool = [...new Set(list(request.security_ids).slice(0, 5))].map(id => state.securities.get(id)).filter(Boolean);
      if (savedDay(request.asof)) byId('comparison-asof').value = request.asof;
      if (['cumulative', 'quarterly', 'annual'].includes(request.period_basis)) byId('comparison-basis').value = request.period_basis;
      if ([20, 60, 120, 252].includes(request.price_window)) byId('comparison-window').value = String(request.price_window);
      if (savedDay(request.financial_period) && request.financial_period <= byId('comparison-asof').value && /-(03-31|06-30|09-30|12-31)$/.test(request.financial_period)) {
        const option = element('option', request.financial_period + ' · 已保存，待读取核对'); option.value = request.financial_period;
        byId('comparison-period').append(option); byId('comparison-period').value = request.financial_period;
      }
    }
    document.querySelectorAll('[data-home-period]').forEach(node => node.setAttribute('aria-pressed', String(node.dataset.homePeriod === state.homePeriod)));
    document.querySelectorAll('[data-indicator]').forEach(node => node.setAttribute('aria-pressed', String(node.dataset.indicator === state.indicator)));
    return {security: state.securities.get(value.security_id), ma};
  } catch { byId('view-preferences-note').textContent = '偏好不可读取，已使用默认视图；不会重放研究或草稿。'; return null; }
}

function element(tag, content, className) {
  const node = document.createElement(tag);
  if (content !== undefined) node.textContent = text(content);
  if (className) node.className = className;
  return node;
}

function button(label, action, className = 'text-button') {
  const node = element('button', label, className);
  node.type = 'button';
  node.addEventListener('click', action);
  return node;
}

function announce(message) {
  clearTimeout(announceTimer);
  byId('live-message').textContent = message;
  announceTimer = setTimeout(() => { byId('live-message').textContent = ''; }, 6500);
}

function connection(message, error = false, retry) {
  const node = byId('connection-state');
  node.classList.toggle('error', error);
  node.replaceChildren(element('span', message));
  if (retry) node.append(button('重试', retry));
}

function empty(container, title, description, retry) {
  const node = element('div', undefined, 'empty-state');
  node.append(element('strong', title));
  if (description) node.append(element('p', description));
  if (retry) {
    node.classList.add('error');
    node.append(button('重新读取', retry, 'button'));
  }
  container.replaceChildren(node);
}

async function api(url, options = {}) {
  let response;
  try {
    response = await fetch(url, {...options, credentials: 'same-origin', headers: {'Accept': 'application/json', ...options.headers}});
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new Error('无法连接本地服务。请确认服务仍在运行后重试。');
  }
  let data;
  try { data = await response.json(); } catch { throw new Error('本地服务返回了无法读取的内容。'); }
  if (!response.ok) {
    const detail = typeof data.error === 'string' ? data.error : data.error?.message;
    const error = new Error(detail || '本地服务未能完成读取，请重试。');
    error.status = response.status;
    throw error;
  }
  return data;
}

function securityLabel(security) {
  return security ? text(security.company || security.code || security.security_id) + (security.name_status === 'historical_name' ? '（本地历史名）' : '') : '尚未选择证券';
}

function securityIdentity(security) {
  if (!security) return '';
  return [security.market, security.exchange, security.code, security.share_class, security.currency].filter(Boolean).join(' · ');
}

function remember(securities) {
  for (const security of list(securities)) {
    if (security?.security_id) state.securities.set(security.security_id, security);
  }
}

function isTracked(securityId) {
  return state.watchlist.some(security => security.security_id === securityId);
}

function researchDate(record) {
  return text(recordRole(record) === 'broker_source' ? record.published_date : record.research_asof).slice(0, 10);
}

function dateLabel(value) {
  if (!value) return '未记录';
  const raw = text(value);
  if (!/^\d{4}-\d{2}-\d{2}T/.test(raw) || !Number.isFinite(Date.parse(raw))) return raw;
  return new Date(raw).toLocaleString('sv-SE', {timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false}) + ' 北京时间';
}

function shortDate(value) {
  return value ? text(value).slice(0, 10) : '无记录';
}

function recordId(record) {
  return text(record.result_id || record.review_id);
}

function recordModule(record) {
  return record.review_id ? 'review' : sourceRoles.has(recordRole(record)) ? recordRole(record) : text(record.module);
}

function recordRole(record) {
  return record.content_role || list(currentContext()?.roles?.classified).find(value => value.result_id === record.result_id)?.content_role || 'unknown';
}

function sortRecords(records) {
  return [...records].sort((left, right) => (Date.parse(researchDate(right)) || 0) - (Date.parse(researchDate(left)) || 0) || (Date.parse(right.generated_at) || 0) - (Date.parse(left.generated_at) || 0) || recordId(right).localeCompare(recordId(left)));
}

function currentEntry() {
  return state.selected ? state.contexts.get(state.selected.security_id) : null;
}

function currentContext() {
  const entry = currentEntry();
  return entry?.status === 'ready' ? entry.data : null;
}

function showPage(name, focus = true) {
  if (!Object.hasOwn(pages, name)) return;
  state.page = name;
  document.querySelectorAll('.page').forEach(page => { page.hidden = page.id !== name; });
  document.querySelectorAll('.sidebar [data-page]').forEach(node => {
    if (node.dataset.page === name || (node.dataset.page === 'company' && companyPages.includes(name))) node.setAttribute('aria-current', 'page');
    else node.removeAttribute('aria-current');
  });
  byId('breadcrumb').textContent = companyPages.includes(name) ? securityLabel(state.selected) + ' / ' + pages[name] : pages[name];
  byId('company-navigation').hidden = !companyPages.includes(name);
  document.querySelectorAll('[data-company-page]').forEach(node => node.setAttribute('aria-pressed', String(node.dataset.companyPage === name)));
  updateFirstResearch();
  renderLocalData();
  if (name === 'company') renderCompanyOverview();
  if (name === 'tasks') { renderTasks(); refreshTasks(true); }
  if (name === 'comparison') renderComparison();
  if (focus) {
    byId(companyPages.includes(name) ? 'company-title' : name + '-title').focus({preventScroll: true});
    window.scrollTo({top: 0, behavior: 'instant'});
  }
  saveViewPreferences();
}

function updateIdentity() {
  byId('current-company').textContent = securityLabel(state.selected);
  byId('current-identity').textContent = securityIdentity(state.selected) || '请从候选中确认完整证券身份';
  const label = state.selected ? securityLabel(state.selected) + ' · ' + text(state.selected.code) : '先选择一只证券';
  byId('overview-company').textContent = label;
  document.querySelectorAll('[data-selected-company]').forEach(node => { node.textContent = label; });
  const tracked = state.selected && isTracked(state.selected.security_id);
  byId('toggle-watchlist').textContent = state.saving ? '正在保存…' : tracked ? '移出关注' : '加入关注';
  byId('toggle-watchlist').disabled = !state.selected || !state.bootstrap?.csrf_token || state.saving;
  byId('toggle-watchlist').setAttribute('aria-pressed', String(Boolean(tracked)));
  document.querySelectorAll('[data-watchlist-count]').forEach(node => { node.textContent = state.watchlist.length + ' 只证券'; });
  if (companyPages.includes(state.page)) byId('breadcrumb').textContent = securityLabel(state.selected) + ' / ' + pages[state.page];
}

function selectSecurity(security, focus = true) {
  clearTimeout(localDataPollTimer);
  state.selection++;
  document.querySelectorAll('dialog').forEach(dialog => { if (dialog.open) dialog.close(); });
  state.selected = security;
  for (const id of ['continuity-status', 'continuity-module', 'continuity-query']) byId(id).value = '';
  state.chartStart = null;
  state.ma = new Set(['ma5']);
  chartView = null;
  byId('technical-chart').replaceChildren();
  byId('chart-readout').replaceChildren();
  byId('technical-provenance').replaceChildren();
  byId('technical-dashboard').hidden = true;
  remember([security]);
  byId('security-query').value = '';
  closeSearch();
  updateIdentity();
  renderData();
  ensureContext(security);
  ensureTechnical(security, state.techPeriod);
  ensureProjection(security, 'fundamental');
  ensureProjection(security, 'analyst');
  ensureProjection(security, 'continuity');
  ensureLocalData(security);
  if (focus) {
    byId('current-company').tabIndex = -1;
    byId('current-company').focus({preventScroll: true});
    announce('当前证券已切换为' + securityLabel(security) + '，' + securityIdentity(security) + '。');
  }
  saveViewPreferences();
}

async function ensureContext(security) {
  if (!security?.security_id) return;
  const known = state.contexts.get(security.security_id);
  if (known) return known.promise;
  const generation = state.generation;
  const entry = {status: 'loading', data: null, error: ''};
  state.contexts.set(security.security_id, entry);
  entry.promise = api('/api/context?security_id=' + encodeURIComponent(security.security_id)).then(data => {
    if (generation !== state.generation) return;
    if ((data.security?.security_id || data.security_id) !== security.security_id) throw new Error('档案返回的证券身份不匹配，已停止展示。');
    entry.status = 'ready';
    entry.data = data;
    renderData();
  }).catch(error => {
    if (generation !== state.generation) return;
    entry.status = 'error';
    entry.error = error.message;
    renderData();
  });
  renderData();
  return entry.promise;
}

function retryContext(security = state.selected) {
  if (!security) return;
  state.contexts.delete(security.security_id);
  ensureContext(security);
}

function contextState(container, emptyTitle = '尚未选择证券') {
  if (!state.selected) {
    empty(container, emptyTitle, '在上方搜索并选择完整证券身份。');
    return true;
  }
  if (!state.bootstrap) {
    empty(container, '本地服务尚未连接', '请重新连接后读取当前证券档案。', initialize);
    return true;
  }
  const entry = currentEntry();
  if (!entry || entry.status === 'loading') {
    empty(container, '正在读取档案…', securityLabel(state.selected));
    return true;
  }
  if (entry.status === 'error') {
    empty(container, '档案读取失败', entry.error, () => retryContext());
    return true;
  }
  return false;
}

function facts(container, rows, className) {
  container.replaceChildren();
  for (const [label, value, note] of rows) {
    const row = element('div');
    const definition = element('dd');
    definition.append(element(className === 'paths' ? 'code' : 'span', value));
    if (note) definition.append(element('span', note));
    row.append(element('dt', label), definition);
    container.append(row);
  }
}

function latestResult(context, module) {
  const selected = context?.roles?.['formal_' + module];
  return selected ? list(context.results).find(record => record.result_id === selected.result_id && record.module === module) : undefined;
}

function renderSummary() {
  const context = currentContext();
  const entry = currentEntry();
  const unread = entry?.status === 'error' ? '读取失败' : !state.bootstrap ? '尚未连接' : state.selected ? '正在读取' : '未选择';
  facts(byId('overview-summary'), [
    ['最近基本面研究', context ? shortDate(latestResult(context, 'fundamental')?.research_asof) : unread],
    ['最近技术面研究', context ? shortDate(latestResult(context, 'technical')?.research_asof) : unread],
    ['全部历史未结项', context ? list(context.pending_items).length + ' 项' : unread]
  ]);
  const project = state.bootstrap?.project || {};
  facts(byId('overview-context'), [
    ['当前证券', state.selected ? text(state.selected.security_id) : '尚未选择'],
    ['正式研究', context ? list(context.results).filter(record => formalRoles.has(recordRole(record))).length + ' 份' : unread],
    ['补充资料', context ? list(context.results).filter(record => sourceRoles.has(recordRole(record))).length + ' 份' : unread],
    ['研究回顾', context ? list(context.reviews).length + ' 份' : unread],
    ['工作空间', project.label || '未配置'],
    ['行情刷新', '不执行'],
    ['分钟研究', '暂停']
  ]);
}

function makeTable(headers, caption) {
  const wrap = element('div', undefined, 'table-wrap');
  const table = element('table');
  const head = element('thead');
  const row = element('tr');
  const body = element('tbody');
  if (caption) table.append(element('caption', caption));
  for (const header of headers) {
    const cell = element('th', header);
    cell.scope = 'col';
    row.append(cell);
  }
  head.append(row);
  table.append(head, body);
  wrap.append(table);
  return {wrap, body};
}

function renderWatchlist(container) {
  if (!state.bootstrap) {
    empty(container, '关注名单尚未读取', '请先连接本地服务。');
    return;
  }
  if (!state.watchlist.length) {
    empty(container, '关注名单为空', '在上方查找已有证券并加入关注。移出关注不会删除其历史档案。');
    return;
  }
  const {wrap, body} = makeTable(['研究对象', '价格走势', '基本面', '技术面', '待验证', '资料截止', '下一件事'], '真实归档快照；价格与MA5共用纵轴，左旧右新。经营、技术分别判断。');
  wrap.querySelector('table')?.classList.add('watchlist-table');
  for (const security of state.watchlist) {
    const entry = state.contexts.get(security.security_id);
    const ready = entry?.status === 'ready';
    const row = element('tr');
    const identity = element('td');
    identity.append(element('strong', securityLabel(security)), element('small', securityIdentity(security)));
    if (state.selected?.security_id === security.security_id) identity.append(element('span', '当前证券', 'tag accent'));
    row.append(identity);
    const price = element('td');
    renderSpark(price, security);
    row.append(price);
    const fallback = entry?.status === 'error' ? '读取失败' : '正在读取…';
    const fundamental = element('td', undefined, 'module-copy');
    const fundamentalResult = ready ? latestResult(entry.data, 'fundamental') : null;
    const financial = state.fundamental.get(security.security_id)?.data;
    fundamental.append(element('strong', financial?.overview?.conclusion || (ready ? fundamentalResult ? '已有基本面研究' : '尚无基本面研究' : fallback)));
    if (fundamentalResult) fundamental.append(element('small', '研究：' + shortDate(fundamentalResult.research_asof)));
    const technical = element('td', undefined, 'module-copy');
    const technicalResult = ready ? latestResult(entry.data, 'technical') : null;
    const dashboard = technicalEntry(security, state.homePeriod, 60)?.data;
    technical.append(element('strong', dashboard?.summary?.title ? periodNames[state.homePeriod] + ' · ' + dashboard.summary.title : (ready ? technicalResult ? '已有技术研究' : '尚无技术研究' : fallback)));
    if (technicalResult) technical.append(element('small', '研究：' + shortDate(technicalResult.research_asof)));
    if (dashboard && dashboard.status === 'diagnostic') technical.append(element('small', '诊断图 · 原资格不变'));
    row.append(fundamental, technical);
    row.append(element('td', ready ? list(entry.data.pending_items).length + ' 项' : fallback));
    const cutoff = element('td');
    cutoff.append(element('small', '经营：' + (financial?.latest?.report_date || '未核')), element('small', '行情：' + (dashboard ? shortDate(dashboard.data_asof) : '待读取')));
    row.append(cutoff);
    const actions = element('td');
    const continuity = state.continuity.get(security.security_id);
    const next = continuity?.status === 'ready' ? list(continuity.data.attention_queue)[0] : null;
    if (next) actions.append(element('small', next.next_step?.label || next.label || next.reason || next.item_id));
    else if (continuity?.status === 'error') actions.append(element('small', '旧判断核对失败'));
    actions.append(button('进入研究 →', () => { selectSecurity(security); showPage('company'); }));
    if (entry?.status === 'error') actions.append(button('重试读取', () => retryContext(security)));
    row.append(actions);
    body.append(row);
  }
  container.replaceChildren(wrap);
}

function readable(record) {
  return Boolean(record.report_id);
}

function openRecord(record, trigger) {
  if (!readable(record)) return;
  readerTrigger = trigger || document.activeElement;
  const title = record.output_kind === 'quantitative_update' ? (record.module === 'fundamental' ? '财务专项快照' : '技术重算快照') : moduleNames[recordModule(record)] || '研究档案';
  byId('reader-title').textContent = title + ' · ' + recordId(record);
  byId('reader-meta').textContent = (recordRole(record) === 'broker_source' ? '原发布：' + publicationLabel(record) + ' ｜ 补充资料，不替代正式研究' : '研究日期：' + dateLabel(record.research_asof) + (recordRole(record) === 'source_adaptation' ? ' ｜ 补充研究解读，不替代正式研究；原研报发布日期见父来源' : '')) + ' ｜ 资料截止：' + dateLabel(record.data_asof) + ' ｜ 生成／导入：' + dateLabel(record.generated_at);
  const assets = byId('reader-assets');
  assets.replaceChildren();
  const attachments = list(record.attachments).filter(attachment => attachment?.relative_path);
  if (attachments.length) {
    assets.append(element('span', '明示附件（新窗口）：'));
    for (const attachment of attachments) {
      const link = element('a', attachment.relative_path);
      link.href = '/api/asset?id=' + encodeURIComponent(record.report_id) + '&path=' + encodeURIComponent(attachment.relative_path);
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      assets.append(link);
    }
  }
  byId('reader-status').textContent = '正在打开归档…';
  clearTimeout(readerTimer);
  readerTimer = setTimeout(() => { byId('reader-status').textContent = '如果正文未显示，请关闭后重新打开，并确认本地服务仍在运行。'; }, 12000);
  byId('report-frame').src = '/api/report?id=' + encodeURIComponent(record.report_id);
  byId('reader-dialog').showModal();
}

function readButton(record) {
  if (!readable(record)) return element('span', '正文不可用', 'muted small');
  const control = button('阅读报告 →', () => openRecord(record, control));
  control.setAttribute('aria-label', '阅读' + (moduleNames[recordModule(record)] || '研究') + '报告 ' + recordId(record));
  return control;
}

function renderReports(container, records, caption, noResults) {
  if (contextState(container)) return;
  if (!records.length) {
    empty(container, noResults || '没有符合条件的档案', '当前证券的其他模块、日期和旧关注项仍可在档案室与研究室查阅。');
    return;
  }
  const {wrap, body} = makeTable(['研究／原发布日期', '模块 / 成果ID', '资料截止', '生成／导入时间', '操作'], caption);
  for (const record of sortRecords(records)) {
    const row = element('tr');
    row.append(element('td', recordRole(record) === 'broker_source' ? publicationLabel(record) : shortDate(record.research_asof), 'date-cell'));
    const title = element('td');
    title.append(element('strong', moduleNames[recordModule(record)] || record.module || '研究档案'), element('small', recordId(record)));
    if (list(record.parent_result_ids).length) title.append(element('small', '前次：' + record.parent_result_ids.join('、')));
    if (recordRole(record) === 'source_adaptation') title.append(element('small', '原研报发布日期见父来源；本条按研究日期排序'));
    if (record.title) title.append(element('small', record.title));
    else if (record.source_title) title.append(element('small', record.source_title));
    if (sourceRoles.has(recordRole(record))) title.append(element('span', '仅补充，不替代正式研究', 'tag'));
    else if (!record.review_id && !formalRoles.has(recordRole(record))) title.append(element('span', '内容角色待核', 'tag warn'));
    row.append(title, element('td', shortDate(record.data_asof), 'date-cell'), element('td', dateLabel(record.generated_at), 'small'));
    const operation = element('td');
    operation.append(readButton(record));
    if (list(record.attachments).length) operation.append(element('small', list(record.attachments).length + ' 个明示附件'));
    row.append(operation);
    body.append(row);
  }
  container.replaceChildren(wrap);
}

function renderModules() {
  const context = currentContext();
  for (const module of ['fundamental', 'technical']) {
    const records = list(context?.results).filter(record => record.module === module && formalRoles.has(recordRole(record)));
    byId(module + '-count').textContent = context ? records.length + ' 份报告' : state.selected ? '待读取' : '未选择证券';
    renderReports(byId(module + '-reports'), records, '按研究日期倒序。原成果和同日修订以不同成果ID保留。', '尚无' + moduleNames[module] + '档案');
  }
}

function renderPending() {
  const container = byId('pending-items');
  const context = currentContext();
  const items = list(context?.pending_items);
  byId('pending-count').textContent = context ? items.length + ' 项' : '待读取';
  if (contextState(container)) return;
  if (!items.length) {
    empty(container, '没有已记录的未结项', '这里只展示档案中的实际记录，不推断未记录的判断或反馈。');
    return;
  }
  const statuses = {pending: '待验证', open: '未结', unknown: '无法判断', needs_evidence: '待补证', monitoring: '持续观察', deferred: '暂缓'};
  const ul = element('ul', undefined, 'item-list');
  for (const item of items) {
    const li = element('li');
    const heading = element('div', undefined, 'item-heading');
    heading.append(element('strong', item.item_id || '未结项'), element('span', statuses[item.status] || item.status || '未记录状态', 'tag warn'));
    li.append(heading, element('p', item.text || item.title || '原记录未提供关注项文字'));
    li.append(element('div', [moduleNames[item.module] || item.module, item.source_result_id || item.result_id, item.type].filter(Boolean).join(' · '), 'item-meta'));
    if (item.data_asof) li.append(element('div', '资料截止：' + dateLabel(item.data_asof), 'item-meta'));
    const details = [];
    if (item.reason) details.push('原因：' + text(item.reason));
    if (list(item.reasons).length) details.push('保留原因：' + item.reasons.map(reason => typeof reason === 'string' ? reason : JSON.stringify(reason)).join('；'));
    if (item.frozen_at) details.push('原始冻结：' + dateLabel(item.frozen_at));
    if (item.report_locator) details.push('报告定位：' + text(item.report_locator));
    if (list(item.applied_review_ids).length) details.push('已应用回顾：' + item.applied_review_ids.join('、'));
    if (details.length) {
      const disclosure = element('details');
      disclosure.append(element('summary', '原记录与回顾依据'), element('p', details.join('\n')));
      li.append(disclosure);
    }
    const source = list(context.results).find(record => record.result_id === (item.source_result_id || item.result_id));
    if (source && readable(source)) li.append(readButton(source));
    ul.append(li);
  }
  container.replaceChildren(ul);
}

function renderReviews() {
  const context = currentContext();
  const records = list(context?.reviews);
  byId('review-count').textContent = context ? records.length + ' 份' : '待读取';
  const container = byId('review-list');
  if (contextState(container)) return;
  if (!records.length) {
    empty(container, '尚无归档回顾', '缺少回顾记录不代表旧判断已解决。');
    return;
  }
  const ul = element('ul', undefined, 'item-list');
  for (const record of sortRecords(records)) {
    const li = element('li');
    li.append(element('h3', recordId(record)), element('div', '研究日期：' + dateLabel(record.research_asof), 'item-meta'));
    if (record.generated_at) li.append(element('div', '生成：' + dateLabel(record.generated_at), 'item-meta'));
    if (record.summary) li.append(element('p', record.summary));
    li.append(readButton(record));
    ul.append(li);
  }
  container.replaceChildren(ul);
}

const continuityLabels = {pending: '待验证', not_due: '尚未到期', needs_evidence: '证据不足', incomparable: '口径不可比', candidate: '待确认候选', reviewed: '已有正式回顾', observation: '历史观察'};
const criterionLabels = {metric: '指标', period: '观察期间', target_period: '目标期间', operator: '比较规则', target: '目标', threshold: '阈值', lower: '下限', upper: '上限', tolerance: '容差', unit: '单位', basis: '口径', price_basis: '价格口径', confirmation: '确认条件', invalidation: '失效条件', next_evidence: '下一项证据', review_due_at: '复核日期', valid_until: '有效截至', deadline: '观察截至', rule: '判定规则', source_locator: '原文定位', statement: '原始表述', kind: '规则类型', timeframe: '周期'};
const continuityReasons = {
  explicit_current_review_required: '需先复核才能用于当前判断', historical_only: '仅供历史回顾', validity_window_ended: '原有效期已结束', review_due: '已到原复核日期', validity_window_unknown: '原有效窗口不明', source_publication_time_unverified: '证据公开时间尚未核实', research_or_data_asof_unknown: '研究或资料截止不明',
  observation_or_hypothesis_is_not_a_numeric_prediction: '观察项或经营假设不属于数值预测', historical_or_imported_item_not_proven_prospective: '历史重建或导入项缺少事前冻结证明', no_frozen_structured_rule: '没有事前冻结的完整数值规则', later_or_unverified_rule_mapping_cannot_be_backdated: '事后适配的规则不能倒填为事前预测', original_archive_not_frozen_before_prediction_deadline: '成果冻结时间不早于原预测截止', outcome_not_later_than_rule_freeze: '结果已知后才冻结规则',
  formal_review_retained_without_new_machine_override: '保留已有正式回顾，机器检查不覆盖', original_observation_window_not_ended: '原观察窗口尚未结束', qualitative_or_scenario_requires_explicit_research_review: '定性假设或情景需结合经营及技术证据复核', no_explicit_structured_rule_no_prose_threshold_inference: '没有明示数值规则，不从自然语言猜阈值', observation_not_prediction_no_hit_rate: '持续观察项不计预测命中率', explicit_same_basis_facts_available: '已有同口径事实可供核对', unit_or_basis_mismatch: '单位或计算口径不一致',
  multiple_explicit_rules_require_selection: '有多个明示规则，需先确认采用哪个', fact_has_no_bound_evidence: '实际值缺少绑定的证据', fact_publication_or_data_asof_unverified: '实际值的公开时间或资料截止未核实', fact_not_available_at_asof: '该事实在本次截止时尚不可得', conflicting_same_basis_facts_require_selection: '同口径实际值互相冲突，需核对原件',
  prospective_frozen_numeric_rule: '原档已冻结的数值规则', not_scored: '不计入预测表现', fulfilled: '达到原规则', not_fulfilled: '未达到原规则', supported: '有支持', falsified: '已证伪', pending: '待验证', monitoring: '持续观察', unknown: '无法判断', cannot_determine: '无法判断', expired: '已过期', withdrawn: '已撤回', not_due: '尚未到期', invalidated: '已失效', not_triggered: '未触发', triggered: '已触发', confirmed: '已确认', partially_fulfilled: '部分兑现',
  archive_qualification: '原档使用资格', explicit_rule: '明示规则', explicit_fact: '明示事实', definition: '原定义', window: '观察窗口', review: '正式回顾',
  source_or_calculation: '资料或计算', operating_assumption: '经营假设', technical_invalidation: '技术条件失效', timing: '时机', execution: '执行', external_or_unknown: '外部因素或原因不明'
};
function continuityReason(value) { const raw = continuityValue(value); return continuityReasons[raw] || (raw.startsWith('item_') ? '原项状态：' + (continuityReasons[raw.slice(5)] || raw.slice(5)) : raw); }

function continuityValue(value) {
  if (value === null || value === undefined || value === '') return '未记录';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (typeof value !== 'object') return text(value);
  if (Array.isArray(value)) return value.length ? value.map(continuityValue).join('；') : '未记录';
  const labels = {...criterionLabels, evidence_id: '证据ID', source_result_id: '来源成果', fact_id: '事实ID', value: '实际值', public_at: '公开日期', public_date: '公开日期标签', public_at_precision: '公开时间精度', financial_period: '财务所属期', data_asof: '资料截止', generated_at: '冻结生成', url: '来源链接', locator: '来源定位', title: '来源标题', text: '证据内容', code: '覆盖说明', source: '来源', report_id: '原报告', attachment: '明示附件', reason: '原因', evidence_ids: '证据ID', evidence: '引用证据', publication_date: '原发布日期', published_at: '公开日期', asof: '资料截止', source_type: '来源类型', fact: '实际值记录'};
  return Object.entries(value).map(([key, val]) => (labels[key] || key) + '：' + (['code', 'reason', 'source'].includes(key) ? continuityReason(val) : continuityValue(val))).join('\n');
}

function continuitySource(item) {
  const id = item.source?.result_id;
  return list(currentContext()?.results).find(record => record.result_id === id) || {...item.source, module: item.source?.module, research_asof: item.original?.research_asof, data_asof: item.original?.data_asof, generated_at: item.original?.generated_at};
}

function prepareContinuityDraft(item) {
  prepareDraft(item.source?.module);
  byId('draft-mode').value = 'continue';
  draftItemIds = [item.item_id];
  byId('draft-question').value = '延续关注项 ' + item.item_id + '：' + (item.original?.text || '') + '\n本轮需解决：' + (item.next_step?.label || item.next_step?.reason || '按原冻结判据补证并复核，不改写原项。');
  byId('draft-scope').value = '原成果：' + (item.source?.result_id || '未记录') + '；实际冻结：' + dateLabel(item.original?.frozen_at) + '；原判据：' + continuityValue(item.original?.definition) + '\n证据缺口：' + list(item.check?.coverage_reasons).join('；');
}

function appendContinuityEvidence(container, values, fallback) {
  if (!list(values).length) { container.append(element('p', fallback, 'hint')); return; }
  const ul = element('ul', undefined, 'continuity-evidence');
  for (const value of values) {
    const row = element('li');
    if (value && typeof value === 'object' && value.evidence_id) {
      row.append(element('strong', continuityValue(value.source || value.title || '来源未记录')));
      const precision = {month: '仅知年月', year: '仅知年份', day: '仅知日期', date: '仅知日期', exact: '已知时间'}[value.public_at_precision];
      row.append(element('p', '证据：' + value.evidence_id + '；公开：' + dateLabel(value.public_at || value.public_date) + (precision ? '（' + precision + '）' : ''), 'hint'));
      if (value.data_asof) row.append(element('p', '资料截止：' + dateLabel(value.data_asof), 'hint'));
      if (value.financial_period) row.append(element('p', '财务所属期：' + value.financial_period, 'hint'));
      if (value.locator) row.append(element('p', '原件定位：' + continuityValue(value.locator), 'hint'));
      const details = element('details'); details.append(element('summary', '完整原证据字段'), element('p', continuityValue(value), 'continuity-definition')); row.append(details);
    } else if (value && typeof value === 'object' && value.code) row.append(element('span', continuityReason(value.code)), element('small', ' · ' + continuityReason(value.source)));
    else row.textContent = continuityValue(value);
    ul.append(row);
  }
  container.append(ul);
}

function continuityCard(item) {
  const article = element('article', undefined, 'continuity-card');
  const status = item.check?.state || 'pending';
  const heading = element('div', undefined, 'item-heading');
  heading.append(element('span', [moduleNames[item.source?.module] || item.source?.module, item.item_id].filter(Boolean).join(' · '), 'item-meta'), element('span', continuityLabels[status] || item.check?.label || status, 'tag ' + (['candidate', 'needs_evidence', 'incomparable'].includes(status) ? 'warn' : status === 'reviewed' ? 'accent' : '')));
  article.append(heading, element('h3', item.original?.text || '原记录没有判断文字'));
  const dates = element('div', undefined, 'continuity-dates');
  for (const [label, value] of [['实际冻结', item.original?.frozen_at], ['原研究截止', item.original?.research_asof], ['原资料截止', item.original?.data_asof], ['复核／观察截至', item.window?.review_due_at || item.window?.deadline || item.window?.valid_until]]) dates.append(element('span', label + '：' + dateLabel(value)));
  article.append(dates);
  const assessment = element('div', undefined, 'continuity-assessment');
  assessment.append(element('strong', item.check?.label || continuityLabels[status] || '需按原规则复核'));
  const reasons = [...list(item.check?.coverage_reasons), ...list(item.effective?.reasons)];
  if (reasons.length) assessment.append(element('p', [...new Set(reasons.map(continuityReason))].join('；')));
  if (status === 'candidate') assessment.append(element('p', '这是本地确定性检查候选，尚未形成正式回顾。', 'hint'));
  if (status === 'candidate' && item.check?.candidate_status) assessment.append(element('p', '候选比较结果：' + continuityReason(item.check.candidate_status) + '；需要人工确认后单独追加回顾。', 'hint'));
  if (!item.prospective?.eligible) assessment.append(element('p', '事前评价受限：' + (list(item.prospective?.reasons).map(continuityReason).join('；') || continuityReason(item.prospective?.qualification) || '缺少可核实的事前冻结或完整规则') + '；不据此计算预测命中率。', 'hint'));
  else assessment.append(element('p', '事前资格：' + (continuityReason(item.prospective.qualification) || '按原冻结记录核验') + '；仍须核对窗口与证据口径。', 'hint'));
  article.append(assessment);
  const columns = element('div', undefined, 'continuity-columns');
  const criterion = element('section'); criterion.append(element('h4', '原判据与观察窗口'), element('p', continuityValue(item.original?.definition), 'continuity-definition'));
  if (item.original?.report_locator) criterion.append(element('p', '原文定位：' + continuityValue(item.original.report_locator), 'hint'));
  if (item.window?.note) criterion.append(element('p', item.window.note, 'hint'));
  if (item.check?.rule) { const adapted = element('details', undefined, 'space'); adapted.append(element('summary', '明示适配规则（不改写原定义）'), element('p', continuityValue(item.check.rule.criterion), 'continuity-definition'), element('p', '规则冻结：' + dateLabel(item.check.rule.rule_generated_at) + '；声明：' + dateLabel(item.check.rule.declared_at), 'hint')); criterion.append(adapted); }
  const evidence = element('section'); evidence.append(element('h4', '后续证据与覆盖'));
  appendContinuityEvidence(evidence, item.evidence?.latest, '没有可用于自动核对的新证据；保留原判断，等待补证。');
  if (list(item.evidence?.facts).length) { evidence.append(element('h4', '明示的同口径实际值')); appendContinuityEvidence(evidence, item.evidence.facts, ''); }
  if (list(item.evidence?.coverage).length) { const details = element('details'); details.append(element('summary', '覆盖范围与适用限制')); appendContinuityEvidence(details, item.evidence.coverage, '覆盖尚未记录'); evidence.append(details); }
  columns.append(criterion, evidence); article.append(columns);
  const next = element('div', undefined, 'continuity-next'); next.append(element('strong', '下一步'), element('p', item.next_step?.label || item.next_step?.reason || '按原冻结条件补充证据后复核。'));
  if (item.next_step?.reason && item.next_step.reason !== item.next_step.label) next.append(element('small', item.next_step.reason));
  article.append(next);
  const details = element('details', undefined, 'continuity-provenance'); details.append(element('summary', '来源、原始证据与正式回顾（' + list(item.reviews).length + '）'));
  details.append(element('p', '来源成果：' + (item.source?.result_id || '未记录') + '；原状态：' + continuityReason(item.original?.status) + '；当前原档状态：' + continuityReason(item.effective?.status), 'hint'));
  if (item.source?.historical_reconstruction) details.append(element('p', '此成果含历史重建；其事后形成日期不能倒填为事前预测。', 'notice space'));
  details.append(element('h4', '原始证据')); appendContinuityEvidence(details, item.evidence?.original, '原始证据未记录');
  details.append(element('h4', '历次正式回顾'));
  if (!list(item.reviews).length) details.append(element('p', '尚无正式回顾；检查候选不会自动改写原项状态。', 'hint'));
  for (const review of list(item.reviews)) {
    const row = element('div', undefined, 'continuity-review');
    row.append(element('strong', review.review_id || '归档回顾'), element('p', '回顾日期：' + dateLabel(review.research_asof || review.generated_at) + '；资料截止：' + dateLabel(review.data_asof) + '；状态：' + continuityReason(review.status), 'hint'), element('p', review.reason || '原回顾未附原因', 'hint'));
    row.append(element('p', '原因分类：' + (list(review.reason_categories).length ? review.reason_categories.map(continuityReason).join('、') : '原回顾未结构化分类，保留原原因，不据文字补造归因'), 'hint'));
    if (list(review.evidence_ids).length) row.append(element('p', '证据ID：' + review.evidence_ids.join('、'), 'hint'));
    if (readable(review)) row.append(readButton({...review, module: 'review'})); details.append(row);
  }
  article.append(details);
  const actions = element('div', undefined, 'actions space');
  const source = continuitySource(item); if (readable(source)) actions.append(readButton(source));
  actions.append(button('准备本项延续研究', () => prepareContinuityDraft(item), 'text-button'));
  if (state.bootstrap?.csrf_token) {
    const review = button('追加人工回顾', () => prepareContinuityReview(item), 'text-button');
    review.disabled = !state.continuity.get(state.selected?.security_id)?.data?.revision_token;
    actions.append(review);
  }
  article.append(actions); return article;
}

function renderContinuityItems(data) {
  const container = byId('continuity-items'), all = list(data.items);
  const status = byId('continuity-status').value, module = byId('continuity-module').value, query = byId('continuity-query').value.trim().toLowerCase();
  const filtered = all.filter(item => (!status || (status === 'reviewed' ? item.check?.has_formal_review || list(item.reviews).length > 0 : item.check?.state === status)) && (!module || item.source?.module === module) && (!query || [item.item_id, item.source?.result_id, item.original?.text, continuityValue(item.original?.definition)].join(' ').toLowerCase().includes(query)));
  byId('continuity-items-count').textContent = filtered.length + ' / ' + all.length + ' 项';
  byId('continuity-filter-note').textContent = '显示' + filtered.length + '项。筛选只影响展示，原始冻结、历史观察和已回顾项均保留。';
  container.replaceChildren();
  if (!filtered.length) { empty(container, all.length ? '没有符合筛选的记录' : '尚无已冻结判断', all.length ? '可清除筛选查看全部历史。' : '无记录不代表全部判断准确；新研究仍需记录可验证的原条件。'); return; }
  for (const item of filtered) container.append(continuityCard(item));
}

function renderContinuity() {
  const status = byId('continuity-state'), dashboard = byId('continuity-dashboard');
  const entry = state.selected && state.continuity.get(state.selected.security_id);
  byId('continuity-refresh').disabled = !state.selected || entry?.status === 'loading';
  dashboard.hidden = true;
  for (const id of ['continuity-summary', 'continuity-queue', 'continuity-items', 'continuity-timeline']) byId(id).replaceChildren();
  if (!state.selected) { empty(status, '先选择一只证券', '确定完整证券身份后，再回顾同一公司的历次研究。'); return; }
  if (!entry || entry.status === 'loading') { empty(status, '正在读取完整研究链…', '同时核对旧项、正式回顾与证据覆盖，不只读取最新报告。'); return; }
  if (entry.status === 'error') { empty(status, '连续研究看板读取失败', entry.error, () => ensureProjection(state.selected, 'continuity', true)); return; }
  const data = entry.data, summary = data.summary || {};
  status.replaceChildren(); dashboard.hidden = false;
  for (const [label, value, note] of [['历史判断与关注项', summary.total_items ?? list(data.items).length, '跨历次成果保留'], ['待确认候选', summary.candidates ?? 0, '未代替正式回顾'], ['证据待补／不可比', (summary.needs_evidence || 0) + (summary.incomparable || 0), '不能算作判断失败'], ['已关联正式回顾', summary.reviewed ?? 0, '按原项逐次追溯']]) byId('continuity-summary').append(metricCard(label, value + ' 项', note));
  const queue = byId('continuity-queue'), attention = list(data.attention_queue);
  byId('continuity-queue-count').textContent = attention.length + ' 项';
  if (!attention.length) empty(queue, '当前没有待处理队列', '仍可查阅完整历史；尚未到期和历史观察不冒充已验证成功。');
  for (const record of attention.slice(0, 6)) {
    const row = element('article', undefined, 'queue-card');
    row.append(element('span', continuityLabels[record.state] || record.state || '待核对', 'tag'), element('h3', record.text || record.label || record.item_id), element('p', record.next_step?.label || record.next_step?.reason || record.reason || '查看原判据后补证。'), element('small', record.item_id));
    const item = list(data.items).find(value => value.item_id === record.item_id && (!record.source_result_id || value.source?.result_id === record.source_result_id));
    if (item) row.append(button('查看原判据 ↓', () => { byId('continuity-status').value = ''; byId('continuity-module').value = ''; byId('continuity-query').value = item.item_id; renderContinuityItems(data); byId('continuity-query').focus({preventScroll: false}); }));
    queue.append(row);
  }
  if (attention.length > 6) queue.append(element('p', '优先显示6项，其余可在下方完整历史中筛选查看。', 'hint'));
  renderContinuityItems(data);
  const timeline = byId('continuity-timeline');
  byId('continuity-timeline-count').textContent = list(data.timeline).length + ' 条';
  if (!list(data.timeline).length) empty(timeline, '尚无研究时间轴');
  for (const record of list(data.timeline)) {
    const row = element('article', undefined, 'timeline-entry');
    const isAdaptation = (record.content_role || record.source?.content_role || recordRole(record)) === 'source_adaptation';
    const isSource = !isAdaptation && (record.event_type === 'source' || record.kind === 'source' || record.type === 'source' || record.content_role === 'broker_source');
    row.append(element('time', (isAdaptation ? '研究日期 · ' : isSource ? '原发布 · ' : '实际生成 · ') + dateLabel(isAdaptation ? record.research_asof : isSource ? record.published_date || record.published_at || record.publication_date || record.source?.published_date : record.generated_at)), element('div', undefined, 'timeline-copy'));
    const copy = row.children[1];
    copy.append(element('strong', isAdaptation ? ['补充研究解读', record.title || record.result_id || record.event_id].filter(Boolean).join(' · ') : record.title || record.summary || [record.label || moduleNames[record.module] || (isSource ? '补充研报' : '归档记录'), record.result_id || record.review_id || record.source_id || record.event_id || record.id].filter(Boolean).join(' · ')), element('p', '研究截止：' + dateLabel(record.research_asof) + '；资料截止：' + dateLabel(record.data_asof) + '；实际生成：' + dateLabel(record.generated_at), 'hint'));
    if (isAdaptation) copy.append(element('p', '原研报发布日期见父来源；本条为后续补充研究解读。', 'hint'));
    if (isSource) copy.append(element('p', '取得：' + dateLabel(record.acquired_at ?? record.source?.acquired_at) + '；提取：' + dateLabel(record.extracted_at ?? record.source?.extracted_at) + '；导入：' + dateLabel(record.imported_at ?? record.source?.imported_at), 'hint'));
    if (record.historical_reconstruction || record.source?.historical_reconstruction) copy.append(element('span', '历史重建／非事前预测', 'tag warn'));
    if (isSource || isAdaptation) copy.append(element('span', '仅补充，不替代正式基本面', 'tag'));
    if (readable(record) && !isSource) copy.append(readButton({...record, module: record.module || (record.event_type === 'review' ? 'review' : record.source?.module), result_id: record.result_id || record.event_id})); timeline.append(row);
  }
  byId('continuity-limitations').textContent = '本次本地核对：' + dateLabel(data.asof) + '。' + list(data.limitations).join('；') + ' 本页不计算总体命中率、收益，也不由日线推断盘中执行先后。';
}

function prepareContinuityReview(item) {
  const entry = state.selected && state.continuity.get(state.selected.security_id);
  if (entry?.status !== 'ready' || !entry.data.revision_token || !state.bootstrap?.csrf_token) return;
  continuityReviewDraft = {security: {...state.selected}, item_id: item.item_id, revision_token: entry.data.revision_token, selection: state.selection, generation: state.generation};
  byId('continuity-review-title').textContent = '追加人工回顾 · ' + item.item_id;
  byId('continuity-review-identity').textContent = securityLabel(state.selected) + ' · ' + securityIdentity(state.selected);
  byId('continuity-review-original').textContent = item.original?.text || '原判断未记录';
  byId('continuity-review-provenance').textContent = '来源：' + (item.source?.result_id || '未记录') + '；实际冻结：' + dateLabel(item.original?.frozen_at) + '；本页核对：' + dateLabel(entry.data.asof);
  byId('continuity-review-status').value = 'keep'; byId('continuity-review-reason').value = '';
  byId('continuity-review-cancel').textContent = '取消';
  byId('continuity-review-status-note').textContent = '只追加有日期的人工回顾，原冻结定义不变。缺少合格事实时不提供“预测成功／失败”操作；也不把候选直接当正式结论。';
  byId('continuity-review-submit').disabled = false;
  byId('continuity-review-dialog').showModal();
}

async function saveContinuityReview(event) {
  event.preventDefault();
  const draft = continuityReviewDraft, note = byId('continuity-review-status-note');
  if (!draft || continuityReviewSaving) return;
  if (draft.selection !== state.selection || draft.generation !== state.generation || draft.security.security_id !== state.selected?.security_id) { note.textContent = '当前证券或档案已改变，请关闭并重新读取原项后回顾。'; return; }
  const reason = byId('continuity-review-reason').value.trim();
  if (!reason || reason.length > 1000) { note.textContent = reason ? '回顾依据与原因须为1000字以内。' : '请填写本次回顾的依据与原因。'; byId('continuity-review-reason').focus(); return; }
  let committed = null;
  continuityReviewSaving = true; byId('continuity-review-submit').disabled = true; note.textContent = '正在追加回顾，原研究保持…';
  try {
    const value = await api('/api/continuity-review', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': state.bootstrap.csrf_token}, body: JSON.stringify({security_id: draft.security.security_id, item_id: draft.item_id, status: byId('continuity-review-status').value, reason, revision_token: draft.revision_token})});
    if (value.saved !== true) throw new Error('服务未确认回顾已保存，请先查阅归档状态。');
    committed = value;
    continuityReviewDraft = null;
    if (draft.generation !== state.generation) return;
    const projectionValid = value.continuity?.security?.security_id === draft.security.security_id;
    const warning = value.warning || (!projectionValid ? '看板暂未重新读取成功；请刷新核查，不要重复提交。' : '');
    state.continuity.set(draft.security.security_id, projectionValid ? {status: 'ready', data: value.continuity, error: ''} : {status: 'error', data: null, error: '回顾已保存，但看板回读待核。请重新读取，不要重复提交回顾。'});
    state.contexts.delete(draft.security.security_id); await ensureContext(draft.security);
    if (draft.selection === state.selection && byId('continuity-review-dialog').open) {
      note.textContent = '回顾已保存：' + (value.review_id || '请查看归档回顾') + '。' + (warning ? warning + ' 请关闭后重新读取看板，不要重复提交。' : '原判断与旧研究均保留。');
      byId('continuity-review-cancel').textContent = '关闭';
      if (!warning) byId('continuity-review-dialog').close();
      announce(warning ? '回顾已保存，看板回读待核；请重新读取，不要重复提交。' : '人工回顾已追加，原判断和原研究保留。');
    }
    renderData();
  } catch (error) {
    if (draft.selection === state.selection && byId('continuity-review-dialog').open) {
      note.textContent = committed ? '回顾已保存：' + (committed.review_id || '请查阅归档') + '；后续看板读取未完成。请重新读取，不要重复提交。' : '回顾未确认写入：' + error.message + '。请先重新核对原项，避免重复提交。';
      if (committed) byId('continuity-review-cancel').textContent = '关闭';
      else await ensureProjection(draft.security, 'continuity', true);
      continuityReviewDraft = null;
    }
  } finally { continuityReviewSaving = false; }
}

function filteredRecords(records, module, dateFrom, dateTo, query = '') {
  const words = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  return records.filter(record => (!module || recordModule(record) === module) && (!dateFrom || researchDate(record) >= dateFrom) && (!dateTo || (researchDate(record) && researchDate(record) <= dateTo)) && words.every(word => [recordId(record), record.title, record.summary, record.conclusion, record.source_title, record.organization, record.security?.company, record.security?.code, state.selected?.company, state.selected?.code].map(text).join(' ').toLocaleLowerCase().includes(word)));
}

function renderArchive() {
  const context = currentContext();
  const module = byId('archive-module').value;
  const dateFrom = byId('archive-from').value;
  const dateTo = byId('archive-to').value;
  const all = [...list(context?.results), ...list(context?.reviews)];
  if (dateFrom && dateTo && dateFrom > dateTo) {
    byId('archive-filter-note').textContent = '起始日期不能晚于截止日期，请调整筛选。';
    byId('archive-count').textContent = '日期范围待调整';
    empty(byId('archive-reports'), '日期范围不正确', '起始日期不能晚于截止日期。');
    return;
  }
  const records = filteredRecords(all, module, dateFrom, dateTo, byId('archive-query').value);
  byId('archive-count').textContent = context ? records.length + ' / ' + all.length + ' 份档案' : '待读取';
  byId('archive-filter-note').textContent = '研究及补充研究解读按研究日期，原研报按原发布日期筛选；同日修订以成果ID区分。' + (context ? ' 当前显示 ' + records.length + ' 份。' : '');
  renderReports(byId('archive-reports'), records, '旧版本保留；原研报和后续补充研究解读分列日期，均不替代正式研究。筛选不会隐藏完整历史旧项。');
}

function renderSettings() {
  const bootstrap = state.bootstrap || {};
  const project = bootstrap.project || {};
  const labels = {codex: 'Codex 执行', deepseek: 'DeepSeek API', ...bootstrap.provider_labels};
  const providers = byId('provider-settings');
  providers.replaceChildren();
  const providerFacts = element('dl', undefined, 'facts');
  const configured = list(bootstrap.providers);
  const availability = researchProviders();
  facts(providerFacts, availability.length ? availability.map(provider => [provider.label || labels[provider.provider], provider.available ? '可用于研究' : '当前不可用', provider.reason]) : (configured.length ? configured : ['codex', 'deepseek']).map(provider => [labels[provider] || provider, '研究来源状态待核']));
  providers.append(providerFacts);
  facts(byId('workspace-settings'), [
    ['项目', project.label || '未配置'],
    ['会话工作目录', project.directory || '未配置', '项目目录与当次研究可写范围分别管理。'],
    ['私有档案根', bootstrap.archive_root || project.archive_root || '使用服务端指定的唯一私有根', '原档不进入源码；查档不创建另一套权威记录。'],
    ['研究版本', '同股不同研究日期独立保留', '同日修订也使用独立成果ID。']
  ], 'paths');
  facts(byId('runtime-settings'), [
    ['工作台版本', bootstrap.version || '待读取'],
    ['当前阶段', bootstrap.phase || '正在连接'],
    ['本地连接', state.bootstrap ? '已连接' : '尚未连接'],
    ['档案读取', state.bootstrap ? '已接入' : '待读取'],
    ['研究草稿', '仅保留本页 · 未执行'],
    ['研究执行', bootstrap.capabilities?.new_research ? '明确开始后执行，完成后校验归档' : '来源未就绪，暂不可开始'],
    ['行情刷新', '不执行'],
    ['分钟研究', '暂停']
  ]);
}

function updateFirstResearch() {
  const context = currentContext();
  const noResearch = Boolean(context && !list(context.results).length && !list(context.reviews).length);
  byId('first-research').hidden = !noResearch || ['settings', 'tasks'].includes(state.page);
  if (noResearch) {
    byId('first-research-title').textContent = securityLabel(state.selected) + ' · 尚无研究档案';
    byId('first-research-identity').textContent = securityIdentity(state.selected) + ' · ' + state.selected.security_id;
  }
}

async function ensureProjection(security, kind, reload = false) {
  if (!security?.security_id || !['fundamental', 'analyst', 'continuity'].includes(kind)) return;
  const cache = state[kind], id = security.security_id;
  if (reload) cache.delete(id);
  if (cache.has(id)) return cache.get(id).promise;
  const generation = state.generation;
  const entry = {status: 'loading', data: null, error: ''};
  cache.set(id, entry);
  const endpoint = {fundamental: '/api/fundamental', analyst: '/api/analyst-updates', continuity: '/api/continuity'}[kind];
  entry.promise = api(endpoint + '?security_id=' + encodeURIComponent(id)).then(data => {
    if (generation !== state.generation) return;
    if (data.security?.security_id !== id) throw new Error('看板返回的证券身份不匹配，已停止展示。');
    entry.status = 'ready'; entry.data = data;
    renderProjections();
  }).catch(error => {
    if (generation !== state.generation) return;
    entry.status = 'error'; entry.error = error.message;
    renderProjections();
  });
  renderProjections();
  return entry.promise;
}

function renderProjections() {
  renderFundamental();
  renderAnalystUpdates();
  renderContinuity();
  renderCompanyOverview();
  renderAttention();
  renderWatchlist(byId('overview-watchlist'));
  if (byId('draft-dialog').open) { renderDraftSources(); updateDraftAvailability(); }
}

function renderAttention() {
  const container = byId('overview-attention');
  container.replaceChildren();
  const sources = state.watchlist.map(security => state.analyst.get(security.security_id)).filter(entry => entry?.status === 'ready' && entry.data.status === 'ok');
  const continuity = state.watchlist.map(security => state.continuity.get(security.security_id)).filter(entry => entry?.status === 'ready');
  const pending = continuity.reduce((sum, entry) => sum + list(entry.data.attention_queue).length, 0);
  const updates = sources.reduce((sum, entry) => sum + (entry.data.summary?.newer_than_formal_count || 0), 0);
  const figures = [
    ['下一步研究关注', continuity.length ? pending + ' 项' : '待读取', '已核对' + continuity.length + ' / ' + state.watchlist.length + '家公司；候选不等于已验证', 'research'],
    ['研报更新线索', sources.length ? updates + ' 条' : '未检查', '相对原研究截止；不代表结论更新', 'fundamental'],
    ['持续跟踪公司', state.watchlist.length + ' 家', '资料和研究截止分别保留', 'company']
  ];
  for (const [label, value, note, page] of figures) {
    const control = button('', () => showPage(page), 'attention-item');
    control.append(element('span', label), element('strong', value), element('small', note));
    container.append(control);
  }
}

function renderCompanyOverview() {
  const f = byId('company-fundamental'), t = byId('company-technical'), checks = byId('company-next-checks');
  f.replaceChildren(); t.replaceChildren(); checks.replaceChildren();
  if (!state.selected) { empty(f, '先选择一只证券'); empty(t, '先选择一只证券'); return; }
  const financial = state.fundamental.get(state.selected.security_id);
  if (financial?.status === 'ready' && financial.data.source) {
    const data = financial.data;
    f.append(element('p', data.overview?.conclusion || '已有正式基本面研究，需结合来源与原报告阅读。', 'overview-conclusion'));
    f.append(element('p', '财务所属期：' + (data.latest?.report_date || '未核') + '；研究截止：' + shortDate(data.source.research_asof), 'hint'));
    f.append(readButton({...data.source, module: 'fundamental'}));
  } else empty(f, financial?.status === 'error' ? '基本面读取失败' : financial?.status === 'ready' ? '暂无可识别正式基本面研究' : '正在读取基本面…', financial?.error);
  const technical = technicalEntry();
  if (technical?.status === 'ready' && technical.data.source) {
    const data = technical.data;
    t.append(element('p', data.summary?.title || '已有技术研究', 'overview-conclusion'));
    t.append(element('p', data.summary?.reason || '', 'muted'));
    t.append(element('p', periodNames[state.techPeriod] + ' · 行情：' + shortDate(data.data_asof) + ' · ' + qualificationLabel(data), 'hint'));
  } else empty(t, technical?.status === 'error' ? '技术面读取失败' : technical?.status === 'ready' ? '暂无技术研究' : '正在读取技术面…', technical?.error);
  const continuity = state.continuity.get(state.selected.security_id);
  if (!continuity || continuity.status === 'loading') { empty(checks, '正在核对历次研究…', '完整旧判断读取完成前，不将未读到当成无项。'); return; }
  if (continuity.status === 'error') { empty(checks, '旧判断核对失败', continuity.error, () => ensureProjection(state.selected, 'continuity', true)); return; }
  const pending = list(continuity.data.attention_queue);
  if (!pending.length) { empty(checks, '当前没有待处理队列', '完整历史仍保留在判断验证；没有队列不代表原判断全部准确。'); return; }
  const ul = element('ul', undefined, 'next-check-list');
  for (const item of pending.slice(0, 3)) {
    const li = element('li');
    li.append(element('strong', item.label || item.item_id), element('p', item.next_step?.label || item.next_step?.reason || item.reason || '按原冻结定义补充下一条证据。', 'hint'), element('small', item.item_id));
    ul.append(li);
  }
  checks.append(ul, element('p', '显示优先查阅的3项；全部' + pending.length + '个下一步及完整历史保留在判断验证中。', 'hint'));
}

function metricCard(label, value, note) {
  const card = element('div', undefined, 'metric-card');
  card.append(element('span', label), element('strong', value), element('small', note));
  return card;
}

function renderFundamental() {
  const status = byId('fundamental-state'), panel = byId('fundamental-dashboard');
  panel.hidden = true;
  const entry = state.selected && state.fundamental.get(state.selected.security_id);
  if (!state.selected) { empty(status, '尚未选择证券', '先从可信候选确认研究对象。'); return; }
  if (!entry || entry.status === 'loading') { empty(status, '正在读取正式基本面研究…', securityLabel(state.selected)); return; }
  if (entry.status === 'error') { empty(status, '基本面看板读取失败', entry.error, () => ensureProjection(state.selected, 'fundamental', true)); return; }
  const data = entry.data;
  if (!data.source || ['no_research', 'unavailable'].includes(data.status)) { empty(status, '暂无可重绘的正式基本面研究', data.reason || '资料补充不会作为正式研究填入本看板。'); return; }
  status.replaceChildren(); panel.hidden = false;
  const source = data.source;
  const provenance = byId('fundamental-provenance'); provenance.replaceChildren();
  for (const [name, value] of [['研究截止', source.research_asof], ['报告生成', source.generated_at], ['财务所属期', data.latest?.report_date], ['原公开日', data.latest?.publish_date]]) {
    const span = element('span', name); span.setAttribute('title', text(value)); span.append(element('strong', dateLabel(value))); provenance.append(span);
  }
  byId('fundamental-thesis').textContent = data.overview?.conclusion || '按已归档正式研究阅读，缺失内容不补造。';
  const companyReference = data.company_research_reference;
  const thesisNote = byId('fundamental-thesis-note');
  thesisNote.textContent = companyReference ? '本轮数值快照：' + source.result_id + '；公司观点沿用原研究，不更新其日期或使用资格。' : '正式研究：' + source.result_id + '。图表沿用各组件实际覆盖，不因研报取得较晚更新主结论。';
  if (companyReference) {
    thesisNote.append(element('span', ' 原公司研究：' + companyReference.result_id + '；原研究截止：' + dateLabel(companyReference.research_asof) + '。'));
    if (companyReference.report_id) { const read = readButton({...companyReference, module: 'fundamental'}); read.textContent = '阅读旧公司研究（未更新）'; thesisNote.append(read); }
  }
  const latest = data.latest || {}, cards = byId('fundamental-cards'); cards.replaceChildren();
  cards.append(metricCard('累计营收', valueLabel(finite(latest.revenue) ? latest.revenue / 1e8 : null) + ' 亿元', latest.report_date || '所属期未核'), metricCard('累计归母净利润', valueLabel(finite(latest.parent_profit) ? latest.parent_profit / 1e8 : null) + ' 亿元', '归属上市公司股东'), metricCard('现金转化率', valueLabel(finite(latest.cash_conversion) ? latest.cash_conversion * 100 : null) + '%', '经营现金流／合并净利润'), metricCard('累计简化自由现金流', valueLabel(finite(latest.fcf) ? latest.fcf / 1e8 : null) + ' 亿元', '经营现金流－购建长期资产现金'));
  const highlights = byId('fundamental-highlights'); highlights.replaceChildren();
  for (const item of list(data.overview?.highlights).slice(0, 3)) { const row = element('div', undefined, 'research-highlight'); row.append(element('strong', item.label || '观察'), element('p', item.text || '')); highlights.append(row); }
  if (!highlights.children.length) highlights.append(element('p', '当前没有可直接引用的结构化阶段解读；请查看原报告。', 'hint'));
  const next = byId('fundamental-next-checks'); next.replaceChildren();
  for (const item of list(data.overview?.next_checks || data.observations).slice(0, 2)) { const row = element('div', undefined, 'condition-item'); row.append(element('p', item.text || ''), element('small', item.definition?.next_evidence || item.item_id || '')); next.append(row); }
  if (!next.children.length) next.append(element('p', '尚无明示冻结关注项。', 'hint'));
  renderFinancialCharts(data);
  const valuation = data.valuation || {}, node = byId('fundamental-valuation'); node.replaceChildren();
  const values = element('dl', undefined, 'facts');
  facts(values, [['原始收盘价', valueLabel(valuation.raw_close) + ' 元'], ['价格日期', valuation.market_date || '未记录'], ['归母TTM盈利倍数', valueLabel(valuation.pe_ttm_parent) + ' 倍'], ['归母TTM利润', valueLabel(finite(valuation.parent_profit_ttm) ? valuation.parent_profit_ttm / 1e8 : null) + ' 亿元']]);
  node.append(values, element('p', valuation.share_basis || '估值股数与股类基础以原报告为准；未核不作跨股比较。', 'notice space'));
  if (list(valuation.scenarios).length) {
    const scenarios = element('details', undefined, 'space'); scenarios.append(element('summary', '原报告估值情景与假设'));
    scenarios.append(element('p', '假设来源：' + (valuation.scenarios_source?.result_id || source.result_id) + '；不因新资料取得自动更新这些假设。', 'hint'));
    for (const item of valuation.scenarios) {
      const row = element('div', undefined, 'scenario-row');
      row.append(element('strong', text(item.name || item.label || '情景') + ' · ' + text(item.horizon || '期限见原报告')));
      row.append(element('p', '收入假设 ' + valueLabel(finite(item.revenue_cny) ? item.revenue_cny / 1e8 : null) + '亿元；归母利润假设 ' + valueLabel(finite(item.parent_profit_cny) ? item.parent_profit_cny / 1e8 : null) + '亿元。', 'hint'));
      if (finite(item.price_low_cny) && finite(item.price_high_cny)) row.append(element('p', '情景价格 ' + valueLabel(item.price_low_cny) + '—' + valueLabel(item.price_high_cny) + '元；不是目标承诺。', 'hint'));
      scenarios.append(row);
    }
    node.append(scenarios);
  }
  const sources = byId('fundamental-sources'); sources.replaceChildren();
  for (const [key, label] of [['history', '历史经营底表'], ['latest', '最新经营资料'], ['valuation', '价格与估值']]) {
    const component = data.components?.[key];
    const row = element('div', undefined, 'component-source');
    row.append(element('strong', label), element('span', component?.result_id || '未取得合格组件'));
    if (component?.report_id) row.append(readButton({...component, module: 'fundamental'}));
    sources.append(row);
  }
  byId('fundamental-limitations').textContent = list(data.limitations).join('；') || '按原研究与各组件实际数据边界阅读。';
}

function financialRows(data) { return list(data[state.finPeriod]); }

function renderFinancialCharts(data) {
  const rows = financialRows(data), svg = byId('fundamental-chart'); svg.replaceChildren();
  document.querySelectorAll('[data-fin-period]').forEach(control => control.setAttribute('aria-pressed', String(control.dataset.finPeriod === state.finPeriod)));
  const quarterly = state.finPeriod === 'quarterly';
  const priceField = rows.some(row => finite(row.reference_close)) ? 'reference_close' : 'raw_close';
  const legend = byId('fundamental-chart-legend'); legend.replaceChildren();
  for (const [label, color, dash] of [['营收', '#9fbdcf'], ['归母净利', '#244e6a'], [priceField === 'reference_close' ? '期末参考价' : '期末原始价', '#cf7d38'], ['已知归母TTM PE', '#7d67a8'], [quarterly ? '归母环比' : '归母同比', '#006f6a'], ['销售毛利率', '#af5c7b']]) {
    const item = element('span', label); item.style.setProperty('--legend-color', color); if (dash) item.className = 'dashed'; legend.append(item);
  }
  if (!rows.length) { svg.append(svgNode('text', {x: 50, y: 80}, '当前没有可核验的' + (quarterly ? '季度' : '年度') + '底表，不补造图形。')); return; }
  const left = 64, right = 810, width = right - left, x = index => left + (index + .5) * width / rows.length;
  function axis(values, top, height, side, color, label, factor = 1, zero = true) {
    const known = values.filter(finite);
    if (!known.length) { svg.append(svgNode('text', {x: side, y: top - 12, fill: color}, label + ' · 缺失')); return null; }
    let low = Math.min(...known, ...(zero ? [0] : [])), high = Math.max(...known, ...(zero ? [0] : []));
    if (high === low) high = low + (Math.abs(low) * .1 || 1);
    const scale = value => top + height - (value - low) / (high - low) * height;
    svg.append(svgNode('text', {x: side, y: top - 12, fill: color}, label));
    for (let tick = 0; tick < 3; tick++) {
      const value = low + (high - low) * tick / 2, yy = scale(value);
      svg.append(svgNode('text', {x: side, y: yy + 4, fill: color, 'text-anchor': side < left ? 'end' : 'start'}, valueLabel(value * factor, 1)));
      if (side < left) svg.append(svgNode('line', {x1: left, x2: right, y1: yy, y2: yy, stroke: '#e5ecef'}));
    }
    return scale;
  }
  const amount = axis(rows.flatMap(row => [row.revenue, row.parent_profit]), 53, 220, 52, '#244e6a', '亿元', 1e-8);
  const price = axis(rows.map(row => row[priceField]), 53, 220, 824, '#cf7d38', '价格 · 元', 1, false);
  const pe = axis(rows.map(row => row.pe_known), 53, 220, 925, '#7d67a8', 'PE · 倍', 1, false);
  const growthField = quarterly ? 'parent_profit_qoq' : 'parent_profit_yoy';
  const growth = axis(rows.map(row => row[growthField]), 340, 100, 52, '#006f6a', '利润增速 %', 100);
  const margin = axis(rows.map(row => row.gross_margin), 340, 100, 824, '#af5c7b', '毛利率 %', 100);
  const barWidth = Math.min(25, width / rows.length * .31);
  rows.forEach((row, index) => {
    if (amount) for (const [field, offset, color] of [['revenue', -.55, '#9fbdcf'], ['parent_profit', .55, '#244e6a']]) if (finite(row[field])) svg.append(svgNode('rect', {x: x(index) + offset * barWidth - barWidth / 2, y: Math.min(amount(row[field]), amount(0)), width: barWidth, height: Math.max(.5, Math.abs(amount(row[field]) - amount(0))), fill: color}));
    const label = quarterly ? row.report_date?.slice(0, 4) + 'Q' + Math.ceil(Number(row.report_date?.slice(5, 7)) / 3) : row.report_date?.slice(0, 4);
    svg.append(svgNode('text', {x: x(index), y: 465, transform: 'rotate(-40 ' + x(index) + ' 465)', 'text-anchor': 'end'}, label || '日期未知'));
  });
  for (const [scale, field, color] of [[price, priceField, '#cf7d38'], [pe, 'pe_known', '#7d67a8'], [growth, growthField, '#006f6a'], [margin, 'gross_margin', '#af5c7b']]) if (scale) svg.append(svgNode('path', {d: seriesPath(rows, row => row[field], x, scale), fill: 'none', stroke: color, 'stroke-width': 2}));
  const first = rows[0]?.report_date, last = rows.at(-1)?.report_date;
  const basis = data.price_basis || {};
  const basisNote = basis.kind === 'diagnostic_chain_reference_not_certified_qfq' ? '链式参考价未逐次核验权息，不是正式前复权或总收益，也不作为市盈率的计算分母。' : text(basis.description || basis.limits || basis.kind || '价格基础详见原组件。');
  byId('fundamental-chart-note').textContent = (quarterly ? '单季金额；利润增速为环比。' : '完整年度金额；利润增速为同比。') + '显示' + first + '至' + last + '，共' + rows.length + '期。各曲线单位及纵轴不同，不能由视觉交叉判断因果。价格／PE缺口留缺，不用原价补参考价；财务柱并不表示期末已经公开。' + basisNote;
  renderCashChart(rows);
  const {wrap, body} = makeTable(['所属期', '原公开日', '营收／归母（亿元）', '经营现金／简化FCF（亿元）', '现金转化／毛利率', '组件来源'], '财务所属期与公开日期分别列示；金额元值换算为亿元，缺失不作为0。');
  for (const row of rows) {
    const tr = element('tr');
    for (const value of [row.report_date || '未知', row.publish_date || '未知', valueLabel(finite(row.revenue) ? row.revenue / 1e8 : null) + ' / ' + valueLabel(finite(row.parent_profit) ? row.parent_profit / 1e8 : null), valueLabel(finite(row.cfo) ? row.cfo / 1e8 : null) + ' / ' + valueLabel(finite(row.fcf) ? row.fcf / 1e8 : null), valueLabel(finite(row.cash_conversion) ? row.cash_conversion * 100 : null) + '% / ' + valueLabel(finite(row.gross_margin) ? row.gross_margin * 100 : null) + '%', row.source?.result_id || '未核']) tr.append(element('td', value));
    body.append(tr);
  }
  byId('fundamental-data-table').replaceChildren(wrap);
}

function renderCashChart(rows) {
  const svg = byId('fundamental-cash-chart'); svg.replaceChildren();
  byId('fundamental-cash-period').textContent = state.finPeriod === 'quarterly' ? '单季度 · 亿元' : '完整年度 · 亿元';
  const values = rows.flatMap(row => [row.cfo, row.fcf]).filter(finite);
  if (!values.length) { svg.append(svgNode('text', {x: 45, y: 70}, '缺少对应现金流，暂不绘图。')); return; }
  const low = Math.min(0, ...values), high = Math.max(0, ...values), range = high - low || 1;
  const y = value => 220 - (value - low) / range * 175, width = 665, x = index => 65 + (index + .5) * width / rows.length, bar = Math.min(24, width / rows.length * .3);
  svg.append(svgNode('line', {x1: 65, x2: 730, y1: y(0), y2: y(0), stroke: '#ced9df'}));
  for (const value of [low, (low + high) / 2, high]) svg.append(svgNode('text', {x: 56, y: y(value) + 4, 'text-anchor': 'end'}, valueLabel(value / 1e8, 1)));
  rows.forEach((row, index) => {
    for (const [field, offset, color] of [['cfo', -.55, '#006f6a'], ['fcf', .55, '#b18238']]) if (finite(row[field])) svg.append(svgNode('rect', {x: x(index) + offset * bar - bar / 2, y: Math.min(y(row[field]), y(0)), width: bar, height: Math.max(.5, Math.abs(y(row[field]) - y(0))), fill: color}));
    if (index === 0 || index === rows.length - 1 || index % Math.max(1, Math.floor(rows.length / 7)) === 0) svg.append(svgNode('text', {x: x(index), y: 245, 'text-anchor': 'middle'}, text(row.report_date).slice(0, 7)));
  });
}

function publicationLabel(record) {
  const date = record.publication_date || record.published_date;
  const precision = record.publication_date_precision || record.publication_precision;
  return date ? date + (precision === 'month' ? '（仅知年月）' : '') : '原发布日期未知';
}

function appendAnalystArchive(container, record, security = state.selected) {
  appendResearchUsage(container, record);
  if (isResearchSource(record)) {
    const update = button('以此研报准备更新研究', () => prepareBrokerDraft(record), 'button compact space');
    update.disabled = security.security_id !== state.selected?.security_id;
    container.append(update);
  } else if (record.content_status === 'text_reviewed' && !record.formal_archive) {
    container.append(element('p', '已核正文尚未正式归档。请先正式归档为补充资料，再发起研报驱动研究。', 'notice space'));
  }
  const outcome = analystArchiveOutcomes.get(security.security_id + '|' + record.source_id);
  const verified = record.formal_archive === true ? record : outcome?.formal_archive === true && ['archived', 'already_archived'].includes(outcome.status) ? outcome : null;
  if (verified) {
    container.append(element('p', '已正式归档为补充研报；归档：' + dateLabel(verified.archived_at) + '；成果：' + (verified.result_id || '未记录') + '。原发布日期保持。', 'hint'));
    return;
  }
  if (outcome?.status === 'partial') container.append(element('p', '原件已保存，补充资料适配尚未完成；未正式归档。' + (outcome.import_id ? '原件导入ID：' + outcome.import_id + '。' : '') + '请按同一来源ID重试完成适配。', 'notice space'));
  else container.append(element('p', '状态：已取得正文，尚未正式归档。工作区暂存不等同正式备份。', 'hint'));
  if (!state.bootstrap?.capabilities?.broker_formal_import) { container.append(element('p', '正式归档尚未开放；需先核验本机宿主对补充资料角色的兼容性。', 'hint')); return; }
  const archive = button(outcome?.status === 'partial' ? '重试完成正式归档' : '正式归档为补充资料', () => archiveAnalystSource(record, security, archive), 'button compact space');
  archive.disabled = !state.bootstrap.csrf_token || analystArchivePending.has(security.security_id + '|' + record.source_id);
  container.append(archive);
}

function appendResearchUsage(container, record) {
  for (const usage of list(record.research_usage).filter(row => row.status === 'considered_in_completed_research')) {
    const section = element('section', undefined, 'notice space');
    section.append(element('strong', '已供本次研究参考'), element('p', '研究完成：' + dateLabel(usage.completed_at)));
    if (typeof usage.coverage === 'string' && usage.coverage) section.append(element('p', usage.coverage));
    const ids = list(usage.result_ids).filter(id => typeof id === 'string');
    if (ids.length) section.append(element('p', '关联成果：' + ids.join('、')));
    section.append(element('p', '供研究参考不代表所有观点已采纳；正式主结论仍须按关联成果角色和日期核对。'));
    container.append(section);
  }
}

async function archiveAnalystSource(record, security, control) {
  if (!security || security.security_id !== state.selected?.security_id || !state.bootstrap?.capabilities?.broker_formal_import || !state.bootstrap.csrf_token || record.formal_archive === true) return;
  const key = security.security_id + '|' + record.source_id;
  if (analystArchivePending.has(key)) return;
  const generation = state.generation, selection = state.selection;
  analystArchivePending.add(key); if (control) control.disabled = true;
  try {
    const value = await api('/api/analyst-archive', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': state.bootstrap.csrf_token}, body: JSON.stringify({security_id: security.security_id, source_id: record.source_id})});
    analystArchiveOutcomes.set(key, value);
    if (generation !== state.generation) return;
    state.contexts.delete(security.security_id);
    await Promise.all([ensureProjection(security, 'analyst', true), ensureProjection(security, 'continuity', true), ensureContext(security)]);
    analystArchivePending.delete(key);
    renderAnalystUpdates();
    if (selection !== state.selection) return;
    if (byId('analyst-dialog').open) {
      const updated = list(state.analyst.get(security.security_id)?.data?.cached_sources).find(source => source.source_id === record.source_id);
      await openAnalystSource(updated || {...record, ...value});
    }
    if (value.status === 'partial') announce('原件已保存，补充资料适配尚未完成，未正式归档。可按原来源ID重试，不创建重复导入。');
    else if (value.formal_archive === true && ['archived', 'already_archived'].includes(value.status)) announce(value.status === 'already_archived' ? '该补充资料已有正式档案，未重复导入。' : '研报已正式归档为补充资料；原发布日期和正式基本面研究保留。');
    else announce('服务尚未确认完整正式归档；请重新读取资料状态后核查。');
  } catch (error) { if (selection === state.selection) announce('正式归档未确认完成：' + error.message + '。可重新读取核对状态。'); }
  finally { analystArchivePending.delete(key); if (control?.isConnected) control.disabled = false; if (generation === state.generation) renderAnalystUpdates(); }
}

function renderAnalystUpdates() {
  const summary = byId('analyst-summary'), container = byId('analyst-updates');
  container.replaceChildren();
  byId('analyst-refresh').disabled = !state.selected || !state.bootstrap?.csrf_token;
  const entry = state.selected && state.analyst.get(state.selected.security_id);
  if (!state.selected) { summary.textContent = '先选择证券，再查看相应研报资料。'; return; }
  if (!entry || entry.status === 'loading') { summary.textContent = '正在核对本地研报索引；正式研究结论保持原样。'; return; }
  if (entry.status === 'error') { summary.textContent = '研报资料读取失败：' + entry.error; container.append(button('重试读取', () => ensureProjection(state.selected, 'analyst', true))); return; }
  const data = entry.data;
  const count = data.summary?.records_total ?? list(data.records).length;
  const checked = data.check_status?.checked_at;
  summary.textContent = data.status !== 'ok' ? '本地研报索引尚不可用，不能据此判断有无新资料；已取得正文仍可独立查阅。' : '索引' + count + '条；其中' + (data.summary?.newer_than_formal_count ?? '未核') + '条发布日期晚于原研究截止。' + (checked ? '最近主动检查：' + dateLabel(checked) + '。' : '尚未主动检查变化；以上为本次索引读取。') + '线索、正文取得与研究更新是不同状态。';
  const cached = list(data.cached_sources);
  if (cached.length) {
    container.append(element('h3', '已取得的补充正文'));
    for (const record of cached) {
      const row = element('article', undefined, 'analyst-source-row');
      row.append(element('strong', record.title || '已取得研报资料'), element('p', '原发布：' + publicationLabel(record) + ' · ' + (record.organization || '机构未核'), 'hint'), element('p', '取得：' + dateLabel(record.acquired_at) + '；仅补充资料，未替代正式基本面研究。', 'hint'));
      row.append(button('查看资料与原文', () => openAnalystSource(record)));
      appendAnalystArchive(row, record);
      container.append(row);
    }
  }
  if (!list(data.records).length) { const notice = element('div'); empty(notice, data.status === 'index_unavailable' ? '本地研报索引不可用' : '当前没有可列示研报线索', list(data.limitations).join('；')); container.append(notice); return; }
  const {wrap, body} = makeTable(['原发布日期', '研报标题与来源', '内容状态', '取得／导入', '操作'], '研报按原发布日期标注，不用发现日或导入日替代；正文只作补充。');
  for (const record of data.records) {
    const row = element('tr'), title = element('td');
    title.append(element('strong', record.title || '标题未知'), element('small', [record.organization, record.authors].filter(Boolean).join(' · ')));
    if (record.newer_than_formal) title.append(element('span', '原研究之后的资料线索', 'tag warn'));
    const cachedId = record.cached_source_id;
    row.append(element('td', publicationLabel(record)), title, element('td', record.content_label || (cachedId ? '已取得公开PDF，仅作补充' : '研报线索，正文未取得')), element('td', record.acquired_at ? dateLabel(record.acquired_at) : cachedId ? '已取得；时间未记录' : '正文未取得'));
    const action = element('td'); action.append(cachedId ? button('查看资料与原文', () => openAnalystSource({...record, source_id: cachedId})) : button('读取公开PDF', () => prepareAnalystFetch(record))); row.append(action); body.append(row);
  }
  container.append(wrap, element('p', list(data.limitations).join('；'), 'hint'));
}

async function refreshAnalyst() {
  const security = state.selected;
  if (!security || !state.bootstrap?.csrf_token) return;
  const generation = state.generation;
  byId('analyst-refresh').disabled = true;
  try {
    await api('/api/analyst-refresh', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': state.bootstrap.csrf_token}, body: JSON.stringify({security_id: security.security_id})});
    if (generation !== state.generation) return;
    await ensureProjection(security, 'analyst', true);
    announce('已检查本地研报资料；正式基本面研究未被替换。');
  } catch (error) { announce('资料检查失败：' + error.message); }
  finally { if (generation === state.generation) byId('analyst-refresh').disabled = !state.selected; }
}

function prepareAnalystFetch(record) {
  if (!state.selected) return;
  const security = {...state.selected}, dialog = byId('analyst-dialog'), content = byId('analyst-dialog-content');
  byId('analyst-dialog-title').textContent = record.title || '取得研报正文'; content.replaceChildren();
  content.append(element('p', '原发布：' + publicationLabel(record) + ' · ' + (record.organization || '机构未核'), 'hint'));
  const form = element('form'), field = element('div', undefined, 'field space'), label = element('label', '公开PDF链接'), input = element('input');
  input.type = 'url'; input.required = true; input.id = 'analyst-source-url'; label.htmlFor = input.id;
  input.placeholder = '粘贴官方公开研报PDF链接'; input.value = record.pdf_url || '';
  field.append(label, input); form.append(field, element('p', '仅支持已核准的公开研报来源；资料暂存不更新正式主结论，也不启动模型研究。', 'hint'));
  const submit = button('取得并暂存补充资料', () => {}, 'button primary space'); submit.type = 'submit'; form.append(submit);
  const status = element('p', '', 'hint'); status.setAttribute('role', 'status'); form.append(status);
  form.addEventListener('submit', async event => {
    event.preventDefault(); submit.disabled = true; status.textContent = '正在取得公开正文…';
    try {
      await api('/api/analyst-fetch', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': state.bootstrap.csrf_token}, body: JSON.stringify({security_id: security.security_id, record_id: record.record_id || record.source_id, url: input.value.trim()})});
      status.textContent = '补充资料已取得；正式研究保持原样。';
      await ensureProjection(security, 'analyst', true);
    } catch (error) { status.textContent = '取得失败：' + error.message; }
    finally { submit.disabled = false; }
  });
  content.append(form); if (!dialog.open) dialog.showModal();
}

async function openAnalystSource(record) {
  if (!state.selected) return;
  const security = {...state.selected}, selection = state.selection;
  const dialog = byId('analyst-dialog'), content = byId('analyst-dialog-content');
  byId('analyst-dialog-title').textContent = record.title || '补充研报资料'; empty(content, '正在读取已取得资料…');
  if (!dialog.open) dialog.showModal();
  try {
    const value = await api('/api/analyst-source?security_id=' + encodeURIComponent(security.security_id) + '&source_id=' + encodeURIComponent(record.source_id));
    if (selection !== state.selection || !dialog.open) return;
    const source = value.source || value;
    content.replaceChildren(element('p', '原发布：' + publicationLabel(source) + '；取得：' + dateLabel(source.acquired_at), 'hint'), element('p', '资料仅作补充，不替代正式研究。原作者观点、预测和实际财务分开核对。', 'notice space'));
    if (source.original_report_date) content.append(element('p', '正文署名日期：' + source.original_report_date + '；索引／公开发布标签：' + publicationLabel(source) + '。', 'hint'));
    if (source.date_note) content.append(element('p', text(source.date_note).replaceAll('document_and_distribution_dates_agree', '封面与公开索引日期一致').replaceAll('document_date_differs_from_distribution_date', '封面落款与公开索引日期不同，分别保留'), 'notice space'));
    for (const note of list(source.ambiguities)) content.append(element('p', note, 'notice space'));
    for (const [label, items] of [['原文观点（待核）', list(value.views || source.views)], ['原文预测（待核）', list(value.forecasts || source.forecasts)]]) {
      const section = element('section', undefined, 'space'); section.append(element('h3', label));
      for (const item of items) {
        let description = typeof item === 'string' ? item : item.text || item.claim;
        if (!description && item.metric) {
          const metric = ({eps: '每股收益', attributable_net_profit: '归母净利润'})[item.metric] || item.metric;
          const number = item.unit === 'CNY' ? valueLabel(finite(item.value) ? item.value / 1e8 : null) + '亿元' : valueLabel(item.value) + (item.unit === 'CNY/share' ? '元/股' : text(item.unit));
          description = text(item.fiscal_year) + '年 ' + metric + '预测：' + number + '；券商预测，尚非实际。';
        }
        section.append(element('p', text(description || '具体内容需核对原文。') + (item.page ? '（原PDF第' + item.page + '页）' : ''), 'hint'));
      }
      if (!items.length) section.append(element('p', '没有已核可提取内容，不从标题补造。', 'hint')); content.append(section);
    }
    const excerpt = value.text_excerpt || source.text_excerpt;
    if (excerpt) { const details = element('details', undefined, 'space'); details.append(element('summary', '正文提取片段'), element('p', excerpt, 'source-excerpt')); content.append(details); }
    if (source.pdf_available || record.pdf_available) {
      const link = element('a', '打开已取得PDF', 'button space'); link.href = '/api/analyst-source-file?security_id=' + encodeURIComponent(security.security_id) + '&source_id=' + encodeURIComponent(record.source_id); link.target = '_blank'; link.rel = 'noopener'; content.append(link);
    }
    const archive = element('section', undefined, 'analyst-archive-status space');
    appendAnalystArchive(archive, {...record, ...source, source_id: record.source_id}, security); content.append(archive);
  } catch (error) { if (selection === state.selection) empty(content, '资料读取失败', error.message); }
}

function technicalKey(security, period, limit = 2000) {
  return security.security_id + '|' + period + '|' + limit;
}

function technicalEntry(security = state.selected, period = state.techPeriod, limit = 2000) {
  return security ? state.technical.get(technicalKey(security, period, limit)) : null;
}

async function ensureTechnical(security, period, limit = 2000) {
  if (!security?.security_id) return;
  const key = technicalKey(security, period, limit);
  const known = state.technical.get(key);
  if (known) return known.promise;
  const generation = state.generation;
  const entry = {status: 'loading', data: null, error: ''};
  state.technical.set(key, entry);
  entry.promise = api('/api/technical?security_id=' + encodeURIComponent(security.security_id) + '&period=' + encodeURIComponent(period) + '&limit=' + limit).then(data => {
    if (generation !== state.generation) return;
    if (data.security?.security_id !== security.security_id || data.period !== period) throw new Error('图表返回的证券或周期不匹配，已停止展示。');
    entry.status = 'ready';
    entry.data = data;
    renderTechnicalPanel();
    renderCompanyOverview();
    renderWatchlist(byId('overview-watchlist'));
    renderWatchlist(byId('research-watchlist'));
  }).catch(error => {
    if (generation !== state.generation) return;
    entry.status = 'error';
    entry.error = error.message;
    renderTechnicalPanel();
    renderCompanyOverview();
    renderWatchlist(byId('overview-watchlist'));
    renderWatchlist(byId('research-watchlist'));
  });
  renderTechnicalPanel();
  renderCompanyOverview();
  return entry.promise;
}

function retryTechnical(security = state.selected, period = state.techPeriod, limit = 2000) {
  if (!security) return;
  state.technical.delete(technicalKey(security, period, limit));
  ensureTechnical(security, period, limit);
}

function svgNode(tag, attributes = {}, content) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, text(value));
  if (content !== undefined) node.textContent = text(content);
  return node;
}

function finite(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

function valueLabel(value, digits = 2) {
  return finite(value) ? value.toLocaleString('zh-CN', {minimumFractionDigits: digits, maximumFractionDigits: digits}) : '缺失';
}

function volumeLabel(value) {
  if (!finite(value)) return '缺失';
  if (Math.abs(value) >= 1e8) return valueLabel(value / 1e8) + '亿';
  if (Math.abs(value) >= 1e4) return valueLabel(value / 1e4) + '万';
  return valueLabel(value, 0);
}

function qualificationLabel(data) {
  const qualifier = data.qualification;
  if (data.status === 'diagnostic') return '诊断用途 · 原价 / 复权资格见来源';
  if (typeof qualifier === 'string') return qualifier;
  return qualifier?.status || qualifier?.level || (data.status === 'ok' ? '按原快照资格展示' : '资格未确认');
}

function renderSpark(container, security) {
  const entry = technicalEntry(security, state.homePeriod, 60);
  if (!entry || entry.status === 'loading') { container.append(element('small', '正在读取价格快照…')); return; }
  if (entry.status === 'error') {
    container.append(element('small', '价格图读取失败'), button('重试', () => retryTechnical(security, state.homePeriod, 60)));
    return;
  }
  const data = entry.data;
  const bars = list(data.bars).slice(-60);
  if (bars.some((bar, index) => index && text(bar.date) <= text(bars[index - 1].date))) {
    container.append(element('small', '价格日期顺序不合规，已停止绘图。'));
    return;
  }
  if (!bars.some(bar => finite(bar.close))) {
    container.append(element('small', data.status === 'no_research' ? '尚无研究 · 无价格图' : data.reason || '暂无可用价格快照'));
    return;
  }
  const wrap = element('div', undefined, 'price-spark');
  const svg = svgNode('svg', {viewBox: '0 0 150 60', role: 'img', 'aria-label': securityLabel(security) + periodNames[state.homePeriod] + '已归档价格与MA5'});
  const values = bars.flatMap(bar => [bar.close, bar.ma5]).filter(finite);
  const low = Math.min(...values), high = Math.max(...values), spread = high - low || Math.abs(high) * .02 || 1;
  const x = index => 3 + index * 144 / Math.max(1, bars.length - 1);
  const y = value => 51 - (value - low) / spread * 44;
  svg.append(svgNode('line', {x1: 0, y1: 55, x2: 150, y2: 55, stroke: '#d5dfe3'}));
  for (const [field, color, width] of [['close', '#3e6c96', 1.8], ['ma5', maColors.ma5, 1.8]]) svg.append(svgNode('path', {d: seriesPath(bars, bar => bar[field], x, y), fill: 'none', stroke: color, 'stroke-width': width, 'stroke-dasharray': field === 'ma5' ? '4 2' : 'none'}));
  const first = bars.find(bar => finite(bar.close)), last = [...bars].reverse().find(bar => finite(bar.close));
  const change = first?.close ? (last.close / first.close - 1) * 100 : null;
  const direction = finite(change) ? (change > 0 ? '上涨 ' : change < 0 ? '下跌 ' : '持平 ') + valueLabel(Math.abs(change)) + '%' : '变化未知';
  svg.append(svgNode('title', {}, periodNames[state.homePeriod] + '最近' + bars.length + '根，' + direction + '；蓝色为收盘价，金色虚线为MA5。'));
  wrap.append(svg, element('small', periodNames[state.homePeriod] + ' · 最近' + bars.length + '根 · ' + direction), element('small', shortDate(bars[0]?.date) + '—' + shortDate(bars.at(-1)?.date)), element('small', data.status === 'diagnostic' ? '诊断快照 · 口径未全核' : '原归档资格'));
  container.append(wrap);
}

function seriesPath(bars, getValue, x, y) {
  let continuous = false;
  return bars.map((bar, index) => {
    if (bar.segment_break) continuous = false;
    const value = getValue(bar);
    if (!finite(value)) { continuous = false; return ''; }
    const segment = (continuous ? 'L' : 'M') + x(index).toFixed(2) + ' ' + y(value).toFixed(2);
    continuous = true;
    return segment;
  }).join(' ');
}

function renderTechnicalPanel() {
  const container = byId('technical-state');
  const entry = technicalEntry();
  const data = entry?.status === 'ready' ? entry.data : null;
  document.querySelectorAll('[data-tech-period]').forEach(control => {
    control.setAttribute('aria-pressed', String(control.dataset.techPeriod === state.techPeriod));
    control.disabled = !state.selected;
  });
  const available = data && list(data.bars).some(bar => finite(bar.open) && finite(bar.high) && finite(bar.low) && finite(bar.close));
  byId('technical-dashboard').hidden = !available;
  if (!available) {
    chartView = null;
    byId('technical-chart').replaceChildren();
    byId('chart-readout').replaceChildren();
    byId('technical-provenance').replaceChildren();
    if (!state.selected) empty(container, '先选择研究证券', '确认完整身份后，读取属于该证券的归档量价与指标。');
    else if (!entry || entry.status === 'loading') empty(container, '正在读取技术快照…', securityLabel(state.selected) + ' · ' + periodNames[state.techPeriod]);
    else if (entry.status === 'error') empty(container, '技术快照读取失败', entry.error, () => retryTechnical());
    else empty(container, data.status === 'no_research' ? '尚无技术研究' : '尚无可重绘的技术序列', data.reason || '保留已有历史报告；没有可用序列时不生成虚构K线。');
    return;
  }
  container.replaceChildren();
  const source = data.source || {};
  const priceBasis = typeof data.adjustment === 'string' ? data.adjustment : data.adjustment?.mode || data.adjustment?.basis || data.adjustment?.price_basis || '见原快照';
  const priceBasisLabel = priceBasis + (data.adjustment?.verified === false ? '（未认证）' : '');
  const provenance = byId('technical-provenance');
  provenance.replaceChildren();
  for (const [name, value] of [['研究截止', source.research_asof], ['价格资料截止', data.data_asof || source.data_asof], ['报告生成', source.generated_at], ['计算生成', source.analysis_generated_at || data.generated_at], ['价格口径', priceBasisLabel], ['使用资格', qualificationLabel(data)], ['周期完成核对', data.period_completion?.verified === true ? '所选期已核（仅声明窗口）' : '未认证／见来源']]) {
    const item = element('span', name);
    item.setAttribute('title', text(value));
    item.append(element('strong', name.includes('截止') || name.includes('生成') ? dateLabel(value) : value));
    provenance.append(item);
  }
  provenance.append(element('span', '周期完成核对不提升价格或复权资格。', 'hint'));
  byId('technical-thesis').textContent = data.summary?.title || '按已归档序列查看量价结构';
  const summaryReason = data.summary?.period !== '1d' && data.summary?.reason?.includes('三条依据') ? '' : data.summary?.reason;
  byId('technical-thesis-note').textContent = [summaryReason, '三条依据与后续条件沿用原报告日线研究；切图不生成新结论。'].filter(Boolean).join(' ');
  byId('technical-explanations').replaceChildren();
  const explanations = list(data.explanations).slice(0, 3);
  for (let index = 0; index < 3; index++) byId('technical-explanations').append(element('li', explanations[index] || '原报告未提供本条说明，保持未知。'));
  const conditions = byId('technical-conditions');
  conditions.replaceChildren();
  if (!list(data.conditions).length) conditions.append(element('p', '原报告没有可用的确认 / 失效条件，待研究补齐。', 'hint'));
  for (const condition of list(data.conditions)) {
    const item = element('div', undefined, 'condition-item');
    item.append(element('p', typeof condition === 'string' ? condition : condition.text || condition.definition || '见原冻结条件'));
    conditions.append(item);
  }
  const states = byId('period-states');
  states.replaceChildren();
  for (const period of Object.keys(periodNames)) {
    const original = list(data.period_states).find(item => item.period === period);
    const row = element('div', undefined, 'period-state');
    const status = element('span', original ? original.status === 'diagnostic' ? '诊断用途' : original.status || '见原资格' : '原快照未提供');
    if (original) {
      status.append(element('small', text(original.bar_count ?? original.source_bar_count ?? '未知') + '根 · ' + shortDate(original.data_asof)));
      status.append(element('small', typeof original.qualification === 'string' ? original.qualification : original.qualification?.status || '资格见原快照'));
      if (original.period_completion_verified === true) status.append(element('small', '完成期已核', 'tag accent'));
    }
    row.append(element('strong', periodNames[period]), status);
    states.append(row);
  }
  byId('technical-qualification').textContent = qualificationLabel(data);
  const sourceBox = byId('technical-source');
  const sourceFacts = element('div', undefined, 'source-facts');
  for (const [label, value] of [['来源成果', source.result_id || '未提供'], ['序列附件', source.attachment || source.attachment_path || '未提供'], ['快照状态', data.source_label || data.source_status || '已归档']]) {
    const item = element('span');
    item.append(element('strong', label), element('span', value));
    sourceFacts.append(item);
  }
  sourceBox.replaceChildren(sourceFacts);
  if (source.report_id) sourceBox.append(readButton({report_id: source.report_id, result_id: source.result_id, module: 'technical', research_asof: source.research_asof, generated_at: source.generated_at, data_asof: source.data_asof, attachments: []}));
  const limitations = list(data.limitations).map(item => typeof item === 'string' ? item : item.reason || JSON.stringify(item));
  if (data.previous_technical) limitations.push('旧新成果是否可比以原快照记录为准，不从重绘图计算研究改善。');
  byId('technical-limitations').textContent = limitations.join('；') || '按原快照资格阅读；图表展示不提升数据可用性或新旧比较资格。';
  document.querySelectorAll('[data-tech-period]').forEach(control => control.setAttribute('aria-pressed', String(control.dataset.techPeriod === state.techPeriod)));
  renderMaOptions(data);
  drawTechnicalChart(data);
}

function renderMaOptions(data) {
  const fieldset = byId('ma-options');
  fieldset.replaceChildren(element('legend', '价格均线'));
  for (const field of Object.keys(maNames)) {
    const available = list(data.bars).some(bar => finite(bar[field]));
    const label = element('label');
    const input = element('input');
    input.type = 'checkbox';
    input.checked = available && state.ma.has(field);
    input.disabled = !available;
    input.setAttribute('aria-label', maNames[field] + (available ? '' : '，原快照样本不足或未提供'));
    input.addEventListener('change', () => { if (input.checked) state.ma.add(field); else state.ma.delete(field); drawTechnicalChart(data); saveViewPreferences(); });
    const title = element('span', maNames[field] + (available ? '' : ' · 缺失'));
    title.style.color = available ? maColors[field] : '';
    label.append(input, title);
    fieldset.append(label);
  }
}

function drawTechnicalChart(data) {
  const all = list(data.bars);
  const count = state.chartWindow === 'all' ? all.length : Math.min(all.length, state.chartWindow);
  const maximum = Math.max(0, all.length - count);
  const start = state.chartStart === null ? maximum : Math.max(0, Math.min(maximum, state.chartStart));
  state.chartStart = start;
  const bars = all.slice(start, start + count);
  const svg = byId('technical-chart');
  svg.replaceChildren();
  const left = 67, right = 920, width = right - left;
  const x = index => left + (index + .5) * width / Math.max(1, bars.length);
  const prices = bars.flatMap(bar => [bar.low, bar.high, ...[...state.ma].map(field => bar[field])]).filter(finite);
  let low = Math.min(...prices), high = Math.max(...prices);
  const padding = (high - low || Math.abs(high) * .02 || 1) * .08;
  low -= padding; high += padding;
  const priceY = value => 313 - (value - low) / (high - low) * 270;
  const add = (tag, attrs, content) => { const node = svgNode(tag, attrs, content); svg.append(node); return node; };
  add('rect', {x: 0, y: 0, width: 1000, height: 600, fill: '#fff'});
  for (let index = 0; index <= 4; index++) {
    const value = low + (high - low) * index / 4, y = priceY(value);
    add('line', {x1: left, y1: y, x2: right, y2: y, stroke: '#e5ecef'});
    add('text', {x: right + 9, y: y + 4}, valueLabel(value));
  }
  add('text', {x: 10, y: 26}, '价格');
  add('text', {x: 10, y: 357}, '成交量');
  add('text', {x: 10, y: 458}, state.indicator === 'macd' ? 'MACD' : 'RSI14');
  const up = '#b74e4c', down = '#238274';
  const candleWidth = Math.max(.7, Math.min(11, width / Math.max(1, bars.length) * .62));
  const volumeMax = Math.max(1, ...bars.map(bar => finite(bar.volume) ? bar.volume : 0));
  const volumeY = value => 425 - value / volumeMax * 62;
  const groups = {up: {wick: [], body: [], volume: []}, down: {wick: [], body: [], volume: []}};
  bars.forEach((bar, index) => {
    if (![bar.open, bar.high, bar.low, bar.close].every(finite)) return;
    const group = bar.close >= bar.open ? groups.up : groups.down;
    const cx = x(index), top = Math.min(priceY(bar.open), priceY(bar.close)), height = Math.max(1.1, Math.abs(priceY(bar.open) - priceY(bar.close)));
    group.wick.push('M' + cx + ' ' + priceY(bar.high) + 'V' + priceY(bar.low));
    group.body.push('M' + (cx - candleWidth / 2) + ' ' + top + 'h' + candleWidth + 'v' + height + 'h' + -candleWidth + 'Z');
    if (finite(bar.volume)) group.volume.push('M' + (cx - candleWidth / 2) + ' 425v' + (volumeY(bar.volume) - 425) + 'h' + candleWidth + 'V425Z');
  });
  for (const [name, color] of [['up', up], ['down', down]]) {
    add('path', {d: groups[name].wick.join(' '), fill: 'none', stroke: color, 'stroke-width': .9});
    add('path', {d: groups[name].body.join(' '), fill: color});
    add('path', {d: groups[name].volume.join(' '), fill: color, opacity: .62});
  }
  for (const field of state.ma) add('path', {d: seriesPath(bars, bar => bar[field], x, priceY), fill: 'none', stroke: maColors[field], 'stroke-width': 1.7, 'stroke-linejoin': 'round'});
  add('line', {x1: left, y1: 337, x2: right, y2: 337, stroke: '#d5dfe3'});
  add('line', {x1: left, y1: 439, x2: right, y2: 439, stroke: '#d5dfe3'});
  add('text', {x: right + 9, y: 372}, volumeLabel(volumeMax));
  add('text', {x: right + 9, y: 426}, '0');
  if (state.indicator === 'macd') {
    const values = bars.flatMap(bar => [bar.macd?.dif, bar.macd?.dea, bar.macd?.histogram]).filter(finite);
    const span = Math.max(.001, ...values.map(Math.abs)) * 1.1;
    const y = value => 510 - value / span * 50;
    add('line', {x1: left, y1: 510, x2: right, y2: 510, stroke: '#c6d1d8'});
    const positive = [], negative = [];
    bars.forEach((bar, index) => {
      const value = bar.macd?.histogram;
      if (!finite(value)) return;
      (value >= 0 ? positive : negative).push('M' + x(index) + ' 510V' + y(value));
    });
    add('path', {d: positive.join(' '), stroke: up, 'stroke-width': candleWidth * .7, opacity: .65});
    add('path', {d: negative.join(' '), stroke: down, 'stroke-width': candleWidth * .7, opacity: .65});
    for (const [field, color] of [['dif', '#006f6a'], ['dea', '#b18238']]) add('path', {d: seriesPath(bars, bar => bar.macd?.[field], x, y), fill: 'none', stroke: color, 'stroke-width': 1.6});
    if (!values.length) add('text', {x: left + 18, y: 505}, 'MACD样本不足或原快照未提供，不补造数值');
    else add('text', {x: right + 9, y: 512}, '0');
  } else {
    const y = value => 562 - value / 100 * 106;
    for (const value of [30, 70]) {
      add('line', {x1: left, y1: y(value), x2: right, y2: y(value), stroke: '#c6d1d8', 'stroke-dasharray': '4 4'});
      add('text', {x: right + 9, y: y(value) + 4}, value);
    }
    add('path', {d: seriesPath(bars, bar => bar.rsi14, x, y), fill: 'none', stroke: '#006f6a', 'stroke-width': 1.8});
    if (!bars.some(bar => finite(bar.rsi14))) add('text', {x: left + 18, y: 505}, 'RSI14样本不足或原快照未提供，不补造数值');
  }
  for (let index = 0; index < Math.min(5, bars.length); index++) {
    const position = Math.round(index * (bars.length - 1) / Math.max(1, Math.min(5, bars.length) - 1));
    add('text', {x: x(position), y: 588, 'text-anchor': index === 0 ? 'start' : index === 4 ? 'end' : 'middle'}, shortDate(bars[position]?.date || bars[position]?.time));
  }
  const crosshair = add('g', {'pointer-events': 'none'});
  chartView = {data, bars, start, x, left, right, width, crosshair, hoverIndex: bars.length - 1};
  updateChartHover(bars.length - 1, false);
  byId('chart-position').max = text(maximum);
  byId('chart-position').value = text(start);
  byId('chart-position').disabled = maximum === 0;
  byId('chart-older').disabled = start === 0;
  byId('chart-newer').disabled = start === maximum;
  byId('chart-window-label').textContent = shortDate(bars[0]?.date || bars[0]?.time) + ' — ' + shortDate(bars.at(-1)?.date || bars.at(-1)?.time);
  byId('period-caption').textContent = periodNames[state.techPeriod] + ' · 显示' + bars.length + '根 / 已载入' + all.length + '根';
  byId('technical-chart-note').textContent = '来源：' + (data.source?.result_id || '归档快照') + '；源周期共' + text(data.source_bar_count ?? all.length) + '根，本页最多载入2000根。MA5在完整周期序列上计算，其他指标沿用原值；缺失值不连线。' + (data.status === 'diagnostic' ? ' 当前仅供诊断，复权与日历等资格未核验完整。' : '');
  document.querySelectorAll('[data-window]').forEach(control => control.setAttribute('aria-pressed', String(text(state.chartWindow) === control.dataset.window)));
}

function updateChartHover(index, drawCrosshair = true) {
  if (!chartView) return;
  const view = chartView;
  index = Math.max(0, Math.min(view.bars.length - 1, index));
  view.hoverIndex = index;
  const bar = view.bars[index];
  const readout = byId('chart-readout');
  readout.replaceChildren(element('strong', shortDate(bar.date || bar.time)));
  const fields = [['开', valueLabel(bar.open)], ['高', valueLabel(bar.high)], ['低', valueLabel(bar.low)], ['收', valueLabel(bar.close)], [view.data.volume_unit === 'unknown' ? '量（单位未核）' : '量', volumeLabel(bar.volume)]];
  for (const field of state.ma) fields.push([maNames[field], valueLabel(bar[field])]);
  if (state.indicator === 'macd') fields.push(['DIF', valueLabel(bar.macd?.dif, 3)], ['DEA', valueLabel(bar.macd?.dea, 3)], ['柱', valueLabel(bar.macd?.histogram, 3)]);
  else fields.push(['RSI14', valueLabel(bar.rsi14)]);
  for (const [name, value] of fields) { const item = element('span', name); item.append(element('b', value)); readout.append(item); }
  view.crosshair.replaceChildren();
  if (drawCrosshair) view.crosshair.append(svgNode('line', {x1: view.x(index), y1: 37, x2: view.x(index), y2: 568, stroke: '#6e8797', 'stroke-width': 1, 'stroke-dasharray': '4 3'}));
}

function setTechnicalPeriod(period) {
  if (!Object.hasOwn(periodNames, period)) return;
  state.techPeriod = period;
  state.chartStart = null;
  state.ma = new Set(['ma5']);
  chartView = null;
  renderTechnicalPanel();
  ensureTechnical(state.selected, period);
  saveViewPreferences();
}

function changeWindow(value) {
  state.chartWindow = value === 'all' ? 'all' : Math.max(10, Number(value));
  state.chartStart = null;
  const data = technicalEntry()?.data;
  if (data) drawTechnicalChart(data);
  saveViewPreferences();
}

function localDataEnabled() { return state.bootstrap?.capabilities?.local_data_update === true; }

function checkedLocalJob(job, security) {
  if (!job || job.output_kind !== 'quantitative_update' || typeof job.update_id !== 'string' || !job.update_id || job.update_id.length > 128 || !Object.hasOwn(localDataStatuses, job.status) || !job.security || ['security_id', 'market', 'exchange', 'code', 'share_class', 'currency'].some(key => job.security[key] !== security[key])) throw new Error('本地更新返回的证券或快照身份不匹配，已停止展示。');
  if (list(job.results).some(row => typeof row.result_id !== 'string' || row.report_id !== 'result:' + row.result_id || !['fundamental', 'technical'].includes(row.module))) throw new Error('本地更新成果引用不完整，请在档案室核对。');
  return job;
}

function refreshLocalResults(job) {
  const key = job.update_id + '|' + list(job.results).map(row => row.result_id).join('|');
  if (!list(job.results).length || localDataResultsSeen.has(key)) return;
  localDataResultsSeen.add(key);
  const security = job.security, sid = security.security_id;
  state.contexts.delete(sid); state.fundamental.delete(sid); state.continuity.delete(sid);
  for (const key of state.technical.keys()) if (key.startsWith(sid + '|')) state.technical.delete(key);
  ensureContext(security); ensureProjection(security, 'fundamental'); ensureProjection(security, 'continuity');
  if (isTracked(sid)) ensureTechnical(security, state.homePeriod, 60);
  if (state.selected?.security_id === sid) ensureTechnical(security, state.techPeriod);
  if (state.comparison.pool.some(row => row.security_id === sid)) invalidateComparison();
}

function pollLocalData(security, job) {
  if (state.selected?.security_id !== security.security_id || !['queued', 'running'].includes(job?.status)) return;
  clearTimeout(localDataPollTimer);
  localDataPollTimer = setTimeout(() => ensureLocalData(security, true), 2500);
}

async function ensureLocalData(security, reload = false) {
  if (!localDataEnabled() || !security?.security_id) return;
  const sid = security.security_id, current = state.localData.get(sid);
  if (!reload && current) return current.promise;
  const generation = state.generation, version = ++localDataReadVersion;
  const entry = {status: 'loading', data: current?.data || null, error: '', version};
  state.localData.set(sid, entry); renderLocalData();
  entry.promise = api('/api/local-data?security_id=' + encodeURIComponent(sid)).then(data => {
    if (generation !== state.generation || state.localData.get(sid)?.version !== version) return;
    if (!data || typeof data.available !== 'boolean') throw new Error('本地更新状态未通过核对。');
    if (data.latest) checkedLocalJob(data.latest, security);
    entry.status = 'ready'; entry.data = data;
    if (data.latest) refreshLocalResults(data.latest);
    pollLocalData(security, data.latest);
  }).catch(error => {
    if (generation !== state.generation || state.localData.get(sid)?.version !== version) return;
    entry.status = 'error'; entry.error = error.message;
  }).finally(() => { if (generation === state.generation) renderLocalData(); });
  return entry.promise;
}

async function operateLocalData(action) {
  const security = state.selected;
  if (!['preview', 'run'].includes(action) || !localDataEnabled() || !security || !state.bootstrap?.csrf_token || localDataPending.has(security.security_id)) return;
  const sid = security.security_id, previous = state.localData.get(sid), job = previous?.data?.latest;
  if (action === 'run' && (!job || !['prepared', 'failed', 'partial', 'interrupted'].includes(job.status))) return;
  const generation = state.generation, version = ++localDataReadVersion;
  const entry = {status: 'loading', data: previous?.data || null, error: '', version};
  state.localData.set(sid, entry); localDataPending.add(sid); clearTimeout(localDataPollTimer); renderLocalData();
  try {
    const result = checkedLocalJob(await api('/api/local-data-' + action, writeOptions(action === 'preview' ? {security_id: sid} : {security_id: sid, update_id: job.update_id})), security);
    if (generation !== state.generation || state.localData.get(sid)?.version !== version) return;
    entry.status = 'ready'; entry.data = {available: true, reason: previous?.data?.reason || '', latest: result};
    refreshLocalResults(result); pollLocalData(security, result);
  } catch (error) {
    if (generation === state.generation && state.localData.get(sid)?.version === version) { entry.status = 'error'; entry.error = '本次操作尚未确认：' + error.message + '。请先读取状态，不要另开重算。'; }
  } finally { localDataPending.delete(sid); renderLocalData(); }
}

function renderLocalData() {
  const control = byId('local-data-check'), panel = byId('local-data-panel'), security = state.selected;
  const entry = security && state.localData.get(security.security_id), data = entry?.data, job = data?.latest;
  const enabled = localDataEnabled(), pending = security && localDataPending.has(security.security_id);
  control.textContent = pending ? '正在处理本地数据…' : '检查本地数据';
  control.hidden = !enabled;
  control.disabled = !security || !enabled || pending || entry?.status === 'loading' || data?.available === false || ['queued', 'running'].includes(job?.status);
  panel.hidden = !enabled || !companyPages.includes(state.page) || !security || !entry || !pending && entry.status !== 'error' && !job && data?.available !== false;
  const status = byId('local-data-state'), results = byId('local-data-results'), actions = byId('local-data-actions');
  status.replaceChildren(); results.replaceChildren(); actions.replaceChildren();
  if (panel.hidden) return;
  if (entry.status === 'error') {
    status.append(element('p', entry.error, 'notice'));
    actions.append(button('读取本地更新状态', () => ensureLocalData(security, true), 'button'));
  }
  if (entry.status === 'loading') status.append(element('p', pending ? '正在处理本次明确操作…' : '正在核对已保存的更新状态…', 'hint'));
  if (data?.available === false) status.append(element('p', data.reason || '本机数据源尚未就绪，未执行重算。', 'hint'));
  if (!job) return;
  status.append(element('strong', securityLabel(job.security) + ' · ' + localDataStatuses[job.status]));
  status.append(element('p', job.stage_label || '按冻结范围执行，原档保留。'));
  status.append(element('p', '检查：' + dateLabel(job.created_at) + '；最近更新：' + dateLabel(job.updated_at), 'hint'));
  const summary = job.summary || {};
  const calendar = summary.calendar_scope;
  const calendarLabel = typeof calendar === 'string' ? calendar : Number.isInteger(calendar?.year) && typeof calendar.matched === 'boolean' ? calendar.year + '年常规交易日' + (calendar.matched ? '已对照，仅声明范围' : '尚未核齐，不能认证完成期') : '完成会话与可得时点分别核对';
  const info = element('dl', undefined, 'facts'); status.append(info);
  facts(info, [
    ['财务所属期', summary.financial_period || '未取得'], ['财务版本', Number.isFinite(summary.financial_versions) ? summary.financial_versions + ' 条' : '未核'],
    ['日线末端', summary.market_last_date || '未取得'], ['日线观察', Number.isFinite(summary.daily_bars) ? summary.daily_bars + ' 根' : '未核'],
    ['周期依据', calendarLabel]]);
  for (const reason of list(summary.limitations)) status.append(element('p', reason, 'hint'));
  if (job.error) status.append(element('p', job.error, 'notice'));
  for (const result of list(job.results)) {
    const archived = list(state.contexts.get(security.security_id)?.data?.results).find(row => row.result_id === result.result_id);
    const row = element('div'); row.append(element('strong', result.module === 'fundamental' ? '财务专项快照' : '技术重算快照'), readButton({...result, ...archived, output_kind: 'quantitative_update'})); results.append(row);
  }
  if (!pending && entry.status === 'ready' && ['prepared', 'failed', 'partial', 'interrupted'].includes(job.status)) actions.append(button(job.status === 'prepared' ? '重算并保存新快照' : '继续同一更新', () => operateLocalData('run'), 'button primary'));
  if (job.status === 'completed') status.append(element('p', '新数值已保存，完整公司观点与旧判断仍须按各自日期和证据核对。', 'hint'));
}

function researchProviders() { return list(state.bootstrap?.research_providers); }
function isResearchSource(source) { return Boolean(source?.source_id && source.content_status === 'text_reviewed' && source.formal_archive === true); }
function taskModule(value) { return value === 'joint' ? '基本面＋技术面' : moduleNames[value] || '研究范围待核'; }
function providerLabel(value) { return researchProviders().find(row => row.provider === value)?.label || ({codex: 'Codex 执行', deepseek: 'DeepSeek API'})[value] || '研究来源待核'; }
function tasksEnabled() { return Boolean(state.bootstrap && Array.isArray(state.bootstrap.research_providers)); }

function prepareDraft(module = 'joint') {
  if (!state.selected) {
    byId('security-query').focus(); searchSecurities();
    announce('先从可信候选中确认证券，再准备首次研究。'); return;
  }
  draftTrigger = document.activeElement;
  state.draftSecurity = {...state.selected};
  draftSubmission = null; draftSourceIds = []; draftItemIds = [];
  const context = currentContext();
  const first = Boolean(context && !list(context.results).length && !list(context.reviews).length);
  byId('draft-title').textContent = securityLabel(state.draftSecurity) + ' · ' + (first ? '首次研究准备' : '研究准备');
  byId('draft-identity').textContent = securityLabel(state.draftSecurity) + ' · ' + securityIdentity(state.draftSecurity);
  byId('draft-module').value = ['fundamental', 'technical', 'joint'].includes(module) ? module : 'joint';
  byId('draft-mode').value = first ? 'full' : 'continue';
  byId('draft-provider').value = researchProviders().find(row => row.available)?.provider || 'codex';
  const today = new Date();
  byId('draft-cutoff').value = today.getFullYear() + '-' + text(today.getMonth() + 1).padStart(2, '0') + '-' + text(today.getDate()).padStart(2, '0');
  byId('draft-question').value = ''; byId('draft-scope').value = '';
  byId('draft-status').textContent = '保存草稿不调用模型。点击开始后，使用所选来源研究并校验归档；实际用量按服务方规则计入当前账户。';
  byId('draft-dialog').showModal();
  renderDraftSources(); updateDraftAvailability();
}

function renderDraftSources() {
  if (!state.draftSecurity) return;
  const mode = byId('draft-mode').value, module = byId('draft-module').value;
  byId('draft-sources-field').hidden = mode !== 'broker_update' && module === 'technical';
  const container = byId('draft-sources'); container.replaceChildren();
  const entry = state.analyst.get(state.draftSecurity.security_id);
  if (!entry || entry.status === 'loading') { container.append(element('p', '正在读取本证券已取得的研报。', 'hint')); return; }
  if (entry.status === 'error') { container.append(element('p', '研报资料读取失败；请回基本面页重新核对。', 'hint')); return; }
  const sources = list(entry.data?.cached_sources).filter(isResearchSource);
  const unarchived = list(entry.data?.cached_sources).filter(source => source.content_status === 'text_reviewed' && !source.formal_archive);
  const selectedCount = draftSourceIds.length;
  draftSourceIds = draftSourceIds.filter(id => sources.some(source => source.source_id === id));
  if (draftSourceIds.length !== selectedCount) byId('draft-status').textContent = '原选资料目前未通过正式归档核对，已取消该选择。请回基本面页核对原件并归档后重新选择。';
  if (unarchived.length) {
    container.append(element('p', '有' + unarchived.length + '份已核正文尚未正式归档，暂不可用于模型研究。请先回基本面页正式归档为补充资料。', 'notice space'));
    container.append(button('返回基本面核对并归档', () => { byId('draft-dialog').close(); showPage('fundamental'); }));
  }
  if (!sources.length) { container.append(element('p', '当前没有已核原页并正式归档的研报正文。研报线索、标题或工作区暂存不能直接用于本次模型研究。', 'hint')); return; }
  for (const source of sources) {
    const label = element('label', undefined, 'source-choice'), input = element('input');
    input.type = 'checkbox'; input.checked = draftSourceIds.includes(source.source_id);
    input.disabled = researchStarting || Boolean(draftSubmission?.uncertain);
    input.addEventListener('change', () => {
      draftSourceIds = input.checked ? [...new Set([...draftSourceIds, source.source_id])] : draftSourceIds.filter(id => id !== source.source_id);
      updateDraftAvailability();
    });
    const copy = element('span', source.title || '已核研报正文');
    copy.append(element('small', publicationLabel(source) + ' · ' + (source.organization || '机构未记录') + ' · ' + (source.formal_archive ? '补充资料已归档' : '已取得并核原页')));
    label.append(input, copy); container.append(label);
  }
}

function updateDraftAvailability() {
  if (!state.draftSecurity || !byId('draft-dialog').open) return;
  const security = state.draftSecurity, context = state.contexts.get(security.security_id);
  const ready = context?.status === 'ready';
  byId('draft-materials').textContent = ready ? '已有成果：' + list(context.data.results).length + '份；回顾：' + list(context.data.reviews).length + '份；历史未结项：' + list(context.data.pending_items).length + '项。原研究保留，本次结果另存。' : '当前档案尚未核对成功，不能把未读到视为无档；请先重新读取。';
  const mode = byId('draft-mode').value;
  const locked = researchStarting || Boolean(draftSubmission?.uncertain);
  for (const id of ['draft-provider', 'draft-mode', 'draft-cutoff', 'draft-question', 'draft-scope']) byId(id).disabled = locked;
  for (const input of byId('draft-sources').querySelectorAll('input')) input.disabled = locked;
  byId('draft-module').disabled = locked || mode === 'broker_update';
  if (mode === 'broker_update') byId('draft-module').value = 'fundamental';
  const selected = researchProviders().find(row => row.provider === byId('draft-provider').value);
  for (const option of byId('draft-provider').querySelectorAll('option')) {
    const provider = researchProviders().find(row => row.provider === option.value);
    option.disabled = !provider?.available;
  }
  byId('draft-start').disabled = locked && !draftSubmission?.uncertain || !ready || !state.bootstrap?.capabilities?.new_research || !selected?.available || security.security_id !== state.selected?.security_id || (mode === 'broker_update' && !draftSourceIds.length);
  byId('draft-start').textContent = researchStarting ? '正在提交…' : draftSubmission?.uncertain ? '原请求重试确认' : '开始研究';
  if (draftSubmission?.uncertain) byId('draft-start').disabled = researchStarting;
  if (!locked && selected && !selected.available) byId('draft-status').textContent = selected.reason || '当前研究来源不可用，请选择另一来源或核对本机配置。';
  if (!locked && !state.bootstrap?.capabilities?.new_research) byId('draft-status').textContent = '研究来源尚未可用；仍可保留草稿和查阅旧研究。请在设置核对来源状态。';
}

function collectDraft() {
  if (!state.draftSecurity || state.draftSecurity.security_id !== state.selected?.security_id) throw new Error('当前证券已改变，请重新准备。');
  return {security: {...state.draftSecurity}, module: byId('draft-module').value, provider: byId('draft-provider').value,
    research_cutoff: byId('draft-cutoff').value, mode: byId('draft-mode').value, question: byId('draft-question').value.trim(),
    scope: byId('draft-scope').value.trim(), source_ids: [...draftSourceIds], item_ids: [...draftItemIds], status: 'draft_not_executed'};
}

function saveDraft(event) {
  event.preventDefault();
  if (researchStarting || draftSubmission?.uncertain) return;
  try { state.draft = collectDraft(); } catch (error) { byId('draft-status').textContent = error.message; return; }
  byId('draft-status').textContent = '草稿已保留在本页，尚未调用模型或形成研究；刷新后不保留。';
  const content = element('dl', undefined, 'facts');
  facts(content, [['本页草稿', securityLabel(state.draft.security)], ['范围', taskModule(state.draft.module)], ['来源', providerLabel(state.draft.provider)], ['方式', taskModes[state.draft.mode]], ['资料截止', state.draft.research_cutoff], ['状态', '尚未执行']]);
  byId('draft-summary').replaceChildren(content, button('继续准备', () => restoreDraft()), button('丢弃本页草稿', () => { state.draft = null; byId('draft-summary').replaceChildren(element('p', '没有已保留的本页草稿。', 'hint')); announce('草稿已丢弃，原研究保持。'); }));
  announce('本页草稿已保留，尚未执行研究。');
}

function restoreDraft() {
  const draft = state.draft; if (!draft) return;
  selectSecurity(draft.security, false); prepareDraft(draft.module);
  for (const [id, field] of [['draft-provider', 'provider'], ['draft-mode', 'mode'], ['draft-cutoff', 'research_cutoff'], ['draft-question', 'question'], ['draft-scope', 'scope']]) byId(id).value = draft[field];
  draftSourceIds = [...draft.source_ids]; draftItemIds = [...draft.item_ids];
  renderDraftSources(); updateDraftAvailability();
}

function prepareBrokerDraft(source) {
  if (!source?.source_id || !state.selected) return;
  if (!isResearchSource(source)) { announce('先在基本面页核对正文并正式归档为补充资料，再准备研报驱动研究。'); return; }
  if (byId('analyst-dialog').open) byId('analyst-dialog').close();
  prepareDraft('fundamental'); byId('draft-mode').value = 'broker_update';
  draftSourceIds = [source.source_id];
  byId('draft-question').value = '核对“' + (source.title || '本份研报') + '”中的新增观点和盈利预测，与已有正式基本面及一手事实分开。说明哪些假设值得跟踪，不能直接将券商预测当已实现业绩。';
  byId('draft-scope').value = '补充研报原发布：' + publicationLabel(source) + '；实际取得：' + dateLabel(source.acquired_at) + '。沿用旧研究及其关注项，核对实质变化与证据缺口。';
  renderDraftSources(); updateDraftAvailability();
}

function validTask(task) {
  if (!task || typeof task.task_id !== 'string' || !task.security || ['security_id', 'market', 'exchange', 'code', 'share_class', 'currency', 'company'].some(key => typeof task.security[key] !== 'string' || !task.security[key]) || !Object.hasOwn(taskStatuses, task.status)) throw new Error('任务返回的身份或状态未通过核对。');
  const known = state.securities.get(task.security.security_id);
  if (known && ['market', 'exchange', 'code', 'share_class', 'currency'].some(key => task.security[key] !== known[key])) throw new Error('任务证券身份不匹配，已停止展示。');
  return task;
}

function acceptTask(task) {
  validTask(task);
  const previous = state.tasks.get(task.task_id);
  if (previous && (previous.security.security_id !== task.security.security_id || ['module', 'provider', 'mode'].some(key => previous[key] !== task[key]))) throw new Error('任务的研究对象或来源发生不一致，已停止更新，请核对原任务。');
  if (previous?.updated_at && task.updated_at && Date.parse(task.updated_at) < Date.parse(previous.updated_at)) return previous;
  const value = {...previous, ...task}; state.tasks.set(task.task_id, value); remember([task.security]);
  const resultKey = task.task_id + '|' + list(task.results).map(row => row.result_id).join('|');
  if (list(task.results).length && !taskResultsSeen.has(resultKey)) {
    taskResultsSeen.add(resultKey);
    const sid = task.security.security_id;
    state.contexts.delete(sid); state.fundamental.delete(sid); state.analyst.delete(sid); state.continuity.delete(sid);
    for (const key of state.technical.keys()) if (key.startsWith(sid + '|')) state.technical.delete(key);
    ensureContext(task.security); ensureProjection(task.security, 'fundamental'); ensureProjection(task.security, 'analyst'); ensureProjection(task.security, 'continuity');
    if (isTracked(sid)) ensureTechnical(task.security, state.homePeriod, 60);
    if (sid === state.selected?.security_id) ensureTechnical(task.security, state.techPeriod);
  }
  return value;
}

function writeOptions(body) { return {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': state.bootstrap.csrf_token}, body: JSON.stringify(body)}; }

async function startResearch() {
  if (researchStarting || !state.bootstrap?.capabilities?.new_research || !state.bootstrap.csrf_token) return;
  if (!draftSubmission) {
    let draft;
    try { draft = collectDraft(); } catch (error) { byId('draft-status').textContent = error.message; return; }
    if (state.contexts.get(draft.security.security_id)?.status !== 'ready') { byId('draft-status').textContent = '先核对本证券已有档案，再开始研究；读取失败不能当成没有旧档。'; return; }
    if (!draft.research_cutoff) { byId('draft-status').textContent = '先填写资料截止日期。'; return; }
    if (draft.mode === 'broker_update' && !draft.source_ids.length) { byId('draft-status').textContent = '研报驱动更新需要明确选择已核原页的正文。'; return; }
    const availableSources = list(state.analyst.get(draft.security.security_id)?.data?.cached_sources).filter(isResearchSource);
    if (draft.source_ids.some(id => !availableSources.some(source => source.source_id === id))) { byId('draft-status').textContent = '所选研报尚未通过正式归档核对；请先回基本面核对原件并归档。'; return; }
    if (!researchProviders().some(row => row.provider === draft.provider && row.available)) { byId('draft-status').textContent = '所选来源尚不可用，请先核对。'; return; }
    const {security, status, ...request} = draft;
    draftSubmission = {payload: {security_id: security.security_id, ...request, idempotency_key: crypto.randomUUID()}, security, uncertain: false};
  }
  const submission = draftSubmission, generation = state.generation;
  researchStarting = true; ++tasksReadVersion; clearTimeout(taskPollTimer); updateDraftAvailability();
  byId('draft-status').textContent = '正在提交研究任务。重复点击不会再创建另一项研究。';
  try {
    const value = await api('/api/research-start', writeOptions(submission.payload));
    const task = validTask(value.task);
    if (task.security.security_id !== submission.security.security_id || task.provider !== submission.payload.provider || task.module !== submission.payload.module) throw new Error('返回的任务与本次研究请求不一致，请在任务页核对。');
    acceptTask(task); state.tasksStatus = 'ready'; state.selectedTaskId = task.task_id;
    draftSubmission = null; state.draft = null; byId('draft-summary').replaceChildren(element('p', '没有尚未执行的本页草稿。', 'hint'));
    if (generation === state.generation) {
      byId('draft-dialog').close(); showPage('tasks'); renderTasks();
      announce(value.reused ? '已找到同一研究任务，继续查看进展。' : '研究任务已提交，可在任务页查看进展。');
    }
  } catch (error) {
    submission.uncertain = !error.status || error.status >= 500;
    if (!submission.uncertain) draftSubmission = null;
    byId('draft-status').textContent = submission.uncertain ? '提交结果暂未确认。原请求已保留，原样重试会复用它；也可到任务页查询，不能把连接失败当成未创建。' : '尚未确认开始：' + error.message;
  } finally { researchStarting = false; updateDraftAvailability(); scheduleTaskPoll(); }
}

function scheduleTaskPoll() {
  clearTimeout(taskPollTimer);
  if (!tasksEnabled() || researchStarting || taskActionPending.size) return;
  if ([...state.tasks.values()].some(task => ['queued', 'running', 'waiting_input', 'cancelling', 'archiving'].includes(task.status))) taskPollTimer = setTimeout(() => refreshTasks(true), 3000);
}

async function refreshTasks(quiet = false) {
  if (!tasksEnabled() || researchStarting || taskActionPending.size) return;
  const generation = state.generation, sequence = ++tasksReadVersion;
  if (!quiet || state.tasksStatus !== 'ready') state.tasksStatus = 'loading';
  renderTasks();
  try {
    const value = await api('/api/research-tasks');
    if (generation !== state.generation || sequence !== tasksReadVersion) return;
    if (!Array.isArray(value.tasks)) throw new Error('任务列表暂未核对成功。');
    for (const task of value.tasks) acceptTask(task);
    if (Array.isArray(value.providers)) { state.bootstrap.research_providers = value.providers; renderSettings(); updateDraftAvailability(); }
    state.tasksStatus = 'ready'; state.tasksError = '';
    if (!state.selectedTaskId) state.selectedTaskId = [...state.tasks.values()].sort((a, b) => text(b.created_at).localeCompare(text(a.created_at)))[0]?.task_id || null;
    if (state.selectedTaskId) {
      const selected = state.selectedTaskId;
      const detail = await api('/api/research-task?task_id=' + encodeURIComponent(selected));
      if (generation !== state.generation || sequence !== tasksReadVersion) return;
      if (detail.task?.task_id !== selected) throw new Error('任务详情身份不匹配。');
      acceptTask(detail.task);
    }
  } catch (error) { if (generation === state.generation && sequence === tasksReadVersion) { state.tasksStatus = 'error'; state.tasksError = error.message; } }
  finally { if (generation === state.generation && sequence === tasksReadVersion) { renderTasks(); scheduleTaskPoll(); } }
}

async function selectResearchTask(taskId) {
  if (!state.tasks.has(taskId)) return;
  state.selectedTaskId = taskId; state.taskRenderKey = ''; renderTasks();
  await refreshTasks(true);
}

function renderTasks() {
  if (!byId('task-list')) return;
  const container = byId('task-list'), status = byId('tasks-state');
  byId('tasks-refresh').disabled = !tasksEnabled() || researchStarting || taskActionPending.size > 0;
  if (!tasksEnabled()) { empty(status, '研究执行尚不可用', '仍可查阅已有成果；请在设置核对研究来源。'); return; }
  if (state.tasksStatus === 'loading') status.replaceChildren(element('p', '正在读取已保存的任务状态…', 'hint'));
  else if (state.tasksStatus === 'error') empty(status, '任务状态读取失败', state.tasksError + '；未确认状态前不会自动重启研究。', () => refreshTasks());
  else status.replaceChildren();
  const tasks = [...state.tasks.values()].sort((a, b) => text(b.created_at).localeCompare(text(a.created_at)));
  const shown = byId('tasks-filter').value === 'selected' ? tasks.filter(task => task.security.security_id === state.selected?.security_id) : tasks;
  byId('task-list-count').textContent = state.tasksStatus === 'ready' ? shown.length + ' 项' : '待读取核对';
  const summary = byId('tasks-summary'); summary.replaceChildren();
  for (const [label, count] of [['研究进行中', tasks.filter(task => ['queued', 'running', 'archiving', 'cancelling'].includes(task.status)).length], ['等待补充', tasks.filter(task => task.status === 'waiting_input').length], ['已完成', tasks.filter(task => task.status === 'completed').length]]) {
    const row = element('div'); row.append(element('small', label), element('strong', state.tasksStatus === 'ready' ? count + ' 项' : '待核对')); summary.append(row);
  }
  container.replaceChildren();
  if (!shown.length) empty(container, state.tasksStatus === 'loading' ? '正在读取…' : state.tasksStatus === 'error' ? '任务列表暂未核对成功' : '当前范围没有研究任务', state.tasksStatus === 'error' ? '读取失败不能当成没有任务，请重新读取后确认。' : '从个股研究选择范围、问题与来源，再明确开始。');
  for (const task of shown) {
    const control = button('', () => selectResearchTask(task.task_id), 'task-list-control');
    control.setAttribute('aria-pressed', String(task.task_id === state.selectedTaskId));
    control.append(element('strong', securityLabel(task.security)), element('small', taskModule(task.module) + ' · ' + (taskModes[task.mode] || '研究') + ' · ' + providerLabel(task.provider)), element('small', dateLabel(task.created_at)), element('span', taskStatuses[task.status], 'tag' + (task.status === 'waiting_input' ? ' warn' : '')));
    if (task.question) control.append(element('small', '问题：' + text(task.question).slice(0, 100)));
    container.append(control);
  }
  const task = state.tasks.get(state.selectedTaskId), detail = byId('task-detail');
  if (!task) { empty(detail, '选择一项研究任务', '任务列表包含不同证券的研究，切股不会取消后台任务。'); return; }
  const key = task.task_id + '|' + task.revision_token + '|' + state.selected?.security_id + '|' + taskActionPending.has(task.task_id) + '|' + state.tasksStatus;
  if (state.taskRenderKey === key) return;
  state.taskRenderKey = key; detail.replaceChildren();
  const heading = element('div', undefined, 'task-detail-heading');
  heading.append(element('h2', securityLabel(task.security)), element('span', taskStatuses[task.status], 'tag accent')); detail.append(heading);
  detail.append(element('p', securityIdentity(task.security) + ' · ' + taskModule(task.module) + ' · ' + providerLabel(task.provider), 'task-detail-subtitle'));
  const phase = element('div', undefined, 'task-stage');
  phase.append(element('strong', task.stage_label || task.phase_label || taskStatuses[task.status]), element('p', '创建：' + dateLabel(task.created_at) + '；最近更新：' + dateLabel(task.updated_at))); detail.append(phase);
  if (task.research_cutoff || task.research_asof) detail.append(element('p', '本次资料截止：' + dateLabel(task.research_asof || task.research_cutoff) + '。原报告与原判断保留，本次成果另存。', 'hint'));
  if (task.question) detail.append(element('p', '本轮研究问题：' + task.question, 'task-selected-note'));
  if (task.security.security_id !== state.selected?.security_id) detail.append(element('p', '当前任务属于' + securityLabel(task.security) + '；顶部选择的是另一证券，研究不会因此改变对象。', 'task-selected-note'));
  const actions = element('div', undefined, 'task-actions'), busy = taskActionPending.has(task.task_id) || state.tasksStatus === 'error';
  if (task.can_cancel) { const cancel = button('取消后续执行', () => taskOperation(task, 'cancel'), 'button'); cancel.disabled = busy; actions.append(cancel); }
  if (task.can_resume) { const resume = button('恢复此研究任务', () => taskOperation(task, 'resume'), 'button primary'); resume.disabled = busy; actions.append(resume); }
  actions.append(button('进入该公司研究', () => { selectSecurity(task.security, false); showPage('company'); }, 'text-button')); detail.append(actions);
  if (['cancelled', 'cancelling', 'partial'].includes(task.status) && list(task.results).length) detail.append(element('p', '已经归档的部分成果仍保留；停止或恢复不会覆盖它们。', 'notice space'));
  if (task.status === 'interrupted') detail.append(element('p', '任务曾中断，尚未自动重新调用来源。核对进展后可显式恢复同一任务。', 'notice space'));
  if (task.warning || task.error) detail.append(element('p', text(task.warning || task.error), 'notice task-error'));
  const pending = list(task.questions).find(question => question.status === 'pending');
  if (pending && task.status === 'waiting_input') renderTaskQuestion(detail, task, pending);
  if (list(task.results).length) {
    detail.append(element('h3', '本次已归档成果', 'space'));
    for (const result of task.results) {
      const row = element('article', undefined, 'task-result');
      row.append(element('h3', sourceRoles.has(result.content_role) ? moduleNames[result.content_role] : taskModule(result.module)), element('p', '成果角色：' + (sourceRoles.has(result.content_role) ? '补充资料，不替代正式主结论' : formalRoles.has(result.content_role) ? '正式研究，日期与资格仍需逐项核对' : '角色待核，暂不作为正式结论'), 'hint'));
      row.append(button('阅读本次成果', async () => {
        await ensureContext(task.security);
        const record = state.contexts.get(task.security.security_id)?.data?.results?.find(record => record.result_id === result.result_id);
        if (!record || record.security?.security_id !== task.security.security_id) { announce('成果已返回，但本证券原件尚未核读成功，请刷新档案后再查看。'); return; }
        openRecord(record);
        byId('reader-meta').textContent = '证券：' + securityLabel(task.security) + ' · ' + securityIdentity(task.security) + ' ｜ ' + byId('reader-meta').textContent;
      })); detail.append(row);
    }
  } else if (task.status === 'completed') detail.append(element('p', '任务已结束，归档成果尚待核对；未据此更新正式研究。', 'notice space'));
  detail.append(element('h3', '实际进展', 'space'));
  const events = element('ol', undefined, 'task-events');
  for (const event of list(task.events).slice(-30).reverse()) {
    const row = element('li'); row.append(element('time', dateLabel(event.at)), element('strong', event.label || '任务进展'));
    if (event.text) row.append(element('p', event.text)); events.append(row);
  }
  if (!list(task.events).length) events.append(element('li', '尚未记录新的研究步骤；不会用估算百分比代替进展。'));
  detail.append(events);
}

function renderTaskQuestion(container, task, question) {
  const form = element('form', undefined, 'task-answer-form'), fieldset = element('fieldset', undefined, 'task-question');
  fieldset.append(element('legend', '需要你补充一项信息'), element('p', question.prompt));
  const key = task.task_id + '|' + question.question_id;
  const options = list(question.options);
  let select;
  if (options.length) {
    const field = element('div', undefined, 'field'), label = element('label', '可选回答'); select = element('select');
    select.id = 'task-answer-option'; label.setAttribute('for', select.id);
    const placeholder = element('option', '请选择，或在下方补充'); placeholder.value = ''; select.append(placeholder);
    for (const option of options) { const row = element('option', option); row.value = option; select.append(row); }
    select.value = taskAnswerDrafts.get(key)?.choice || '';
    select.addEventListener('change', () => taskAnswerDrafts.set(key, {...taskAnswerDrafts.get(key), choice: select.value})); field.append(label, select); fieldset.append(field);
  }
  const field = element('div', undefined, 'field'), label = element('label', options.length ? '补充说明（可选）' : '你的补充（必填）'), input = element('textarea');
  input.id = 'task-answer-text'; input.rows = 3; input.maxLength = 4000; label.setAttribute('for', input.id); input.value = taskAnswerDrafts.get(key)?.text || '';
  input.addEventListener('input', () => taskAnswerDrafts.set(key, {...taskAnswerDrafts.get(key), text: input.value})); field.append(label, input); fieldset.append(field); form.append(fieldset);
  form.append(element('p', '回答只用于这项任务；提交后继续同一证券、同一来源的研究。'));
  const status = element('p', '', 'task-answer-status'); status.setAttribute('role', 'status');
  const submit = element('button', '提交补充并继续', 'button primary space'); submit.type = 'submit'; submit.disabled = taskActionPending.has(task.task_id) || state.tasksStatus !== 'ready'; form.append(submit, status);
  form.addEventListener('submit', async event => {
    event.preventDefault(); const parts = [select?.value, input.value.trim()].filter(Boolean), answer = parts.join('\n补充：');
    if (!answer) { status.textContent = '请先选择一个回答或填写补充信息。'; input.focus(); return; }
    if (answer.length > 4000) { status.textContent = '回答请控制在4000字以内。'; return; }
    submit.disabled = true; status.textContent = '正在保存补充信息并继续原任务…';
    await taskOperation(task, 'answer', {question_id: question.question_id, answers: answer});
  }); container.append(form);
}

async function taskOperation(task, action, extra = {}) {
  if (taskActionPending.has(task.task_id) || !state.bootstrap?.csrf_token) return;
  taskActionPending.add(task.task_id); ++tasksReadVersion; clearTimeout(taskPollTimer); renderTasks();
  try {
    const value = await api('/api/research-' + action, writeOptions({task_id: task.task_id, revision_token: task.revision_token, ...extra}));
    if (value.task?.task_id !== task.task_id) throw new Error('返回的任务身份不匹配，请刷新核对。');
    acceptTask(value.task);
    announce(({cancel: '取消请求已保存；已归档部分保留。', resume: '正在恢复同一研究任务，不创建重复成果。', answer: '补充信息已提交，继续原研究任务。'})[action]);
  } catch (error) { announce('任务操作尚未确认：' + error.message + '。正在重新核对状态，请勿连续提交。'); }
  finally { taskActionPending.delete(task.task_id); state.taskRenderKey = ''; await refreshTasks(true); renderTasks(); }
}


function comparisonValue(value, unit) {
  if (!finite(value)) return '缺数';
  if (unit === 'CNY') return valueLabel(value / 1e8) + ' 亿元';
  if (unit === 'ratio') return valueLabel(value * 100) + '%';
  return valueLabel(value) + ({multiple: ' 倍', 'CNY/share': ' 元/股', percentage_points: ' 个百分点', count: ''}[unit] ?? (' ' + text(unit)));
}

function comparisonAdjustment(technical) {
  const adjustment = technical?.adjustment, mode = typeof adjustment === 'string' ? adjustment : adjustment?.mode;
  return mode ? mode + (adjustment?.verified === true ? '（价格映射已核）' : '（未认证）') : '价格模式未记录';
}

function invalidateComparison(resetPeriod = false) {
  comparisonReadVersion++;
  comparisonController?.abort();
  state.comparison.status = 'idle'; state.comparison.data = null; state.comparison.error = '';
  comparisonRenderKey = '';
  if (resetPeriod) { const option = element('option', '自动选择最近共同期'); option.value = ''; byId('comparison-period').replaceChildren(option); byId('comparison-period').value = ''; }
  renderComparison();
  saveViewPreferences();
}

function addComparison(security) {
  const pool = state.comparison.pool;
  if (!security?.security_id || pool.some(row => row.security_id === security.security_id)) return;
  if (pool.length >= 5) { announce('比较池最多5只，请先移除一只。'); return; }
  pool.push({...security}); invalidateComparison(true);
}

function comparisonRequest() {
  return {security_ids: state.comparison.pool.map(row => row.security_id), asof: byId('comparison-asof').value,
    period_basis: byId('comparison-basis').value, financial_period: byId('comparison-period').value,
    price_window: Number(byId('comparison-window').value)};
}

function validateComparison(data, request) {
  const ids = request.security_ids, sameIds = rows => rows.length === ids.length && new Set(rows).size === ids.length && ids.every(id => rows.includes(id));
  if (data?.schema_version !== 'stock-research-comparison/1' || !sameIds(list(data.request?.security_ids)) ||
      data.request.asof_date !== request.asof || data.request.period_basis !== request.period_basis || data.request.price_window !== request.price_window ||
      !sameIds(list(data.securities).map(row => row.security_id)) || (request.financial_period && data.financial_period !== request.financial_period)) throw new Error('比较返回的证券、日期或口径不一致，已停止展示。');
  for (const row of data.securities) {
    const expected = state.comparison.pool.find(sec => sec.security_id === row.security_id);
    if (!row.security || ['security_id', 'market', 'exchange', 'code', 'share_class', 'currency'].some(key => row.security[key] !== expected[key])) throw new Error('比较返回的完整证券身份不一致。');
  }
  for (const section of list(data.sections)) for (const row of list(section.rows)) {
    if (!Object.hasOwn(comparisonLabels, row.comparability) || !sameIds(list(row.values).map(value => value.security_id)) ||
        row.values.some(value => value.value !== null && !finite(value.value))) throw new Error('比较指标缺少身份绑定或包含无效数值。');
  }
  const price = data.price_series;
  if (price && ['comparable', 'diagnostic'].includes(price.status)) {
    const dates = list(price.dates);
    if (price.rebased_to !== 100 || !sameIds(list(price.series).map(row => row.security_id)) || dates.length < 2 ||
        dates.some((day, index) => !/^\d{4}-\d{2}-\d{2}$/.test(day) || index && day <= dates[index - 1]) ||
        price.series.some(row => !Array.isArray(row.values) || row.values.length !== dates.length || row.values.some(value => value !== null && !finite(value)))) throw new Error('共同窗口价格序列未通过日期与身份核对。');
  }
}

async function loadComparison() {
  const request = comparisonRequest();
  if (request.security_ids.length < 2 || request.security_ids.length > 5 || !/^\d{4}-\d{2}-\d{2}$/.test(request.asof)) {
    state.comparison.status = 'error'; state.comparison.error = '请加入2—5只可信证券，并填写资料观察截止日。'; renderComparison(); return;
  }
  const version = ++comparisonReadVersion, generation = state.generation, signature = JSON.stringify(request);
  comparisonController?.abort(); comparisonController = new AbortController();
  state.comparison.status = 'loading'; state.comparison.data = null; state.comparison.error = ''; comparisonRenderKey = ''; renderComparison();
  const query = '?security_ids=' + encodeURIComponent(request.security_ids.join(',')) + '&asof=' + encodeURIComponent(request.asof) + '&period_basis=' + encodeURIComponent(request.period_basis) + '&price_window=' + request.price_window + (request.financial_period ? '&financial_period=' + encodeURIComponent(request.financial_period) : '');
  try {
    const data = await api('/api/comparison' + query, {signal: comparisonController.signal});
    if (version !== comparisonReadVersion || generation !== state.generation || signature !== JSON.stringify(comparisonRequest())) return;
    validateComparison(data, request);
    state.comparison.data = data; state.comparison.status = 'ready';
    const periods = byId('comparison-period'); periods.replaceChildren();
    const automatic = element('option', '自动选择最近共同期' + (data.financial_period ? ' · ' + data.financial_period : '')); automatic.value = ''; periods.append(automatic);
    for (const period of list(data.financial_periods)) { const option = element('option', period); option.value = period; periods.append(option); }
    periods.value = request.financial_period;
  } catch (error) {
    if (error.name === 'AbortError' || version !== comparisonReadVersion || generation !== state.generation) return;
    state.comparison.status = 'error'; state.comparison.error = error.message;
  }
  if (version === comparisonReadVersion && generation === state.generation) renderComparison();
}

async function openComparisonSource(ref, securityId, trigger) {
  const security = state.comparison.pool.find(row => row.security_id === securityId);
  if (!security || !ref.result_id || ref.report_id !== 'result:' + ref.result_id) { announce('该来源缺少可核报告身份，请先核对资料。'); return; }
  const version = comparisonReadVersion, selection = state.selection;
  await ensureContext(security);
  if (version !== comparisonReadVersion || selection !== state.selection || state.page !== 'comparison') return;
  const record = state.contexts.get(securityId)?.data?.results?.find(row => row.result_id === ref.result_id);
  if (!record || record.security?.security_id !== securityId || record.report_id !== ref.report_id) { announce('来源报告尚未在对应证券档案中核对成功。'); return; }
  openRecord(record, trigger);
  byId('reader-meta').textContent = '证券：' + securityLabel(security) + ' · ' + securityIdentity(security) + ' ｜ ' + byId('reader-meta').textContent;
}

function comparisonRefs(container, refs, securityId) {
  if (!list(refs).length) return;
  const details = element('details', undefined, 'comparison-evidence'); details.append(element('summary', '核对依据与原报告'));
  for (const ref of refs) {
    const row = element('div'); row.append(element('small', [ref.result_id, ref.attachment, ref.field_path].filter(Boolean).join(' · ')));
    row.append(element('small', '研究：' + dateLabel(ref.research_asof) + '；生成：' + dateLabel(ref.generated_at) + '；数据：' + dateLabel(ref.data_asof)));
    if (ref.sha256) row.append(element('small', '附件指纹：' + text(ref.sha256)));
    if (ref.report_id && ref.result_id) {
      const control = button('阅读依据报告', () => openComparisonSource(ref, securityId, control)); row.append(control);
    }
    details.append(row);
  }
  container.append(details);
}

function renderComparisonMetrics(data) {
  const container = byId('comparison-financial-chart'); container.replaceChildren();
  const rows = list(data.sections).filter(section => ['operating', 'cash'].includes(section.id)).flatMap(section => list(section.rows));
  const selected = ['revenue', 'parent_profit', 'cfo', 'cash_conversion'].map(metric => rows.find(row => row.metric === metric)).filter(Boolean);
  for (const row of selected) {
    const card = element('article', undefined, 'comparison-metric'); card.append(element('h3', row.label));
    if (!['comparable', 'diagnostic'].includes(row.comparability)) { card.append(element('p', row.reason || '当前口径不可比，不绘制比较柱。', 'hint')); container.append(card); continue; }
    const values = state.comparison.pool.map(security => row.values.find(cell => cell.security_id === security.security_id));
    const valid = values.filter(cell => finite(cell.value) && ['ok', 'diagnostic'].includes(cell.status)).map(cell => cell.value);
    if (!valid.length) { card.append(element('p', '没有可绘制数值，缺数不作为零。', 'hint')); container.append(card); continue; }
    const low = Math.min(0, ...valid), high = Math.max(0, ...valid), span = high - low || 1, left = 66, width = 250, x = value => left + (value - low) / span * width;
    const svg = svgNode('svg', {viewBox: '0 0 460 ' + (values.length * 34 + 34), role: 'img', 'aria-label': row.label + '，各证券分别展示，单位与下表一致'});
    svg.append(svgNode('line', {x1: x(0), x2: x(0), y1: 10, y2: values.length * 34 + 9, stroke: '#cdd9df'}));
    values.forEach((cell, index) => {
      const y = 20 + index * 34;
      svg.append(svgNode('text', {x: 0, y: y + 4}, state.comparison.pool[index].code));
      if (finite(cell.value) && ['ok', 'diagnostic'].includes(cell.status)) svg.append(svgNode('rect', {x: Math.min(x(0), x(cell.value)), y: y - 8, width: Math.max(.5, Math.abs(x(cell.value) - x(0))), height: 15, fill: comparisonColors[index]}));
      svg.append(svgNode('text', {x: 328, y: y + 4}, comparisonValue(cell.value, row.unit)));
    });
    card.append(svg); if (row.comparability === 'diagnostic') card.append(element('small', '仅供诊断；' + text(row.reason)));
    container.append(card);
  }
  if (!selected.length) container.append(element('p', '本次没有可展示的共同财务重点，详见分项与不可比原因。', 'hint'));
}

function renderComparisonPrice(data) {
  const chart = byId('comparison-price-chart'), legend = byId('comparison-price-legend'), price = data.price_series || {};
  chart.replaceChildren(); legend.replaceChildren();
  byId('comparison-price-status').textContent = comparisonLabels[price.status] || '资料不足';
  byId('comparison-price-note').textContent = [price.reason, typeof price.basis === 'string' ? price.basis : '', list(price.dates).length ? price.dates[0] + ' — ' + price.dates.at(-1) + '；起点100，共同观察点等距。' : '', data.securities.map(row => row.security.code + '原技术计算：' + comparisonAdjustment(row.technical)).join('；'), '本次价格比较与各自原技术计算分开，不按不同价链的原技术指标高低排名。', price.status === 'diagnostic' ? '诊断归一不提高复权、公司行为或交易日历资格，不作严格投资回报排名。' : ''].filter(Boolean).join(' ');
  if (!['comparable', 'diagnostic'].includes(price.status) || !list(price.series).length || list(price.dates).length < 2) { empty(chart, '暂不叠加价格曲线', price.reason || '缺少同口径共同价格窗口。'); return; }
  const all = price.series.flatMap(row => row.values).filter(finite), low = Math.min(100, ...all), high = Math.max(100, ...all), span = high - low || 1;
  const x = index => 60 + index / (price.dates.length - 1) * 700, y = value => 250 - (value - low) / span * 210;
  const svg = svgNode('svg', {viewBox: '0 0 800 300', role: 'img', 'aria-label': '同口径共同窗口价格指数，起点100。' + text(price.reason)});
  for (const value of [low, (low + high) / 2, high]) { svg.append(svgNode('line', {x1: 60, x2: 760, y1: y(value), y2: y(value), stroke: '#e1e8eb'}), svgNode('text', {x: 51, y: y(value) + 4, 'text-anchor': 'end'}, valueLabel(value, 1))); }
  svg.append(svgNode('line', {x1: 60, x2: 760, y1: y(100), y2: y(100), stroke: '#9daeb8', 'stroke-dasharray': '4 4'}));
  state.comparison.pool.forEach((security, index) => {
    const series = price.series.find(row => row.security_id === security.security_id); let path = '', drawing = false;
    series.values.forEach((value, point) => { if (!finite(value)) { drawing = false; return; } path += (drawing ? ' L' : ' M') + x(point).toFixed(2) + ' ' + y(value).toFixed(2); drawing = true; });
    svg.append(svgNode('path', {d: path, fill: 'none', stroke: comparisonColors[index], 'stroke-width': 2}));
    const item = element('span'); const swatch = element('i'); swatch.style.background = comparisonColors[index]; item.append(swatch, element('span', securityLabel(security))); legend.append(item);
  });
  svg.append(svgNode('text', {x: 60, y: 281}, price.dates[0]), svgNode('text', {x: 760, y: 281, 'text-anchor': 'end'}, price.dates.at(-1)));
  chart.append(svg);
}

function renderComparison() {
  const comparison = state.comparison, pool = comparison.pool;
  byId('comparison-count').textContent = pool.length + ' / 5只';
  const added = pool.some(row => row.security_id === state.selected?.security_id);
  byId('comparison-add-current').disabled = !state.selected || added || pool.length >= 5;
  byId('comparison-add-current').textContent = added ? '当前证券已在比较池' : '将当前证券加入比较';
  const candidate = byId('comparison-candidate'), value = candidate.value;
  candidate.replaceChildren(); const placeholder = element('option', '选择证券'); placeholder.value = ''; candidate.append(placeholder);
  for (const security of state.securities.values()) if (!pool.some(row => row.security_id === security.security_id)) { const option = element('option', securityLabel(security) + ' · ' + security.code + ' · ' + security.exchange); option.value = security.security_id; candidate.append(option); }
  candidate.value = value;
  byId('comparison-add-candidate').disabled = pool.length >= 5 || !candidate.value;
  byId('comparison-run').disabled = !state.bootstrap || pool.length < 2 || pool.length > 5 || comparison.status === 'loading';
  const nodes = byId('comparison-pool'); nodes.replaceChildren();
  for (const [index, security] of pool.entries()) {
    const card = element('div', undefined, 'comparison-security'); const dot = element('i'); dot.style.background = comparisonColors[index];
    const copy = element('div'); copy.append(element('strong', securityLabel(security)), element('small', securityIdentity(security)));
    const remove = button('移除', () => { comparison.pool = comparison.pool.filter(row => row.security_id !== security.security_id); invalidateComparison(true); }); remove.setAttribute('aria-label', '从比较池移除' + securityLabel(security)); card.append(dot, copy, remove); nodes.append(card);
  }
  if (!pool.length) nodes.append(element('p', '先将2—5只证券加入比较池。', 'hint'));
  const key = comparisonReadVersion + '|' + comparison.status + '|' + text(comparison.error);
  if (key === comparisonRenderKey) return;
  comparisonRenderKey = key;
  byId('comparison-results').hidden = comparison.status !== 'ready';
  const status = byId('comparison-state'); status.replaceChildren();
  if (comparison.status === 'loading') { empty(status, '正在核对共同期间与口径…', '只读取已归档资料，不发起模型或新取数。'); return; }
  if (comparison.status === 'error') { empty(status, '比较暂未完成', comparison.error, loadComparison); return; }
  if (comparison.status !== 'ready') { empty(status, '比较尚未读取', '确认对象、观察截止和口径后，点击“读取同口径比较”。改变条件后旧结果会清除。'); return; }
  const data = comparison.data;
  byId('comparison-cutoff').textContent = '资料观察截止：' + dateLabel(data.asof) + '；共同财报所属期：' + (data.financial_period || '没有共同可核期间') + '。原报告日期与行情末端分别保留。';
  byId('comparison-qualification').textContent = '分项比较 · 不设综合总分';
  byId('comparison-financial-period').textContent = data.financial_period || '共同期间缺失';
  const conclusions = byId('comparison-conclusions'); conclusions.replaceChildren();
  for (const conclusion of list(data.conclusions)) {
    const card = element('article', undefined, 'comparison-conclusion'); card.append(element('p', conclusion.text));
    if (list(conclusion.limits).length) card.append(element('small', conclusion.limits.join('；')));
    for (const basis of list(conclusion.basis_refs)) comparisonRefs(card, basis.source_refs, basis.security_id);
    conclusions.append(card);
  }
  if (!list(data.conclusions).length) conclusions.append(element('p', '资料尚不足以形成共同结论，请先核对缺数及不可比原因。', 'hint'));
  const sources = byId('comparison-sources'); sources.replaceChildren();
  for (const row of data.securities) {
    const card = element('article'); card.append(element('h3', securityLabel(row.security)), element('small', securityIdentity(row.security)));
    if (row.reason) card.append(element('p', row.reason));
    const dates = element('dl', undefined, 'facts'); facts(dates, [['财报所属期', row.financial?.period || '未取得'], ['来源版本公开日', row.financial?.publish_date || '未核'], ['基本面研究截止', dateLabel(row.financial?.research_asof)], ['基本面生成', dateLabel(row.financial?.generated_at)], ['行情资料截止', dateLabel(row.technical?.data_asof)], ['原技术计算口径', comparisonAdjustment(row.technical)], ['技术研究生成', dateLabel(row.technical?.generated_at)]]); card.append(dates);
    card.append(element('p', '来源版本公开日不直接认定为该财报期首次披露日。原技术计算保留各自价链，下方价格比较采用本图单列口径。', 'hint'));
    const technical = row.technical?.summary; if (technical) card.append(element('p', '技术状态：' + (typeof technical === 'string' ? technical : technical.title || technical.reason || '需核原报告')));
    for (const [label, source] of [['基本面', row.financial?.source], ['技术面', row.technical?.source]]) if (source?.result_id) { card.append(element('small', label + '：' + source.result_id)); comparisonRefs(card, [source], row.security_id); }
    sources.append(card);
  }
  renderComparisonMetrics(data); renderComparisonPrice(data);
  const sections = byId('comparison-sections'); sections.replaceChildren();
  for (const section of list(data.sections)) {
    const card = element('section', undefined, 'box comparison-section'); card.append(element('h2', section.label));
    const {wrap, body} = makeTable(['指标 / 口径', ...pool.map(securityLabel), '可比性与原因'], section.label + '；金额亿元、比率百分比，分项口径分别核对，不按单一指标评投资潜力。');
    for (const row of list(section.rows)) {
      const tr = element('tr'), label = element('th'); label.scope = 'row'; label.append(element('strong', row.label)); if (row.formula) label.append(element('small', row.formula)); tr.append(label);
      for (const security of pool) {
        const value = row.values.find(cell => cell.security_id === security.security_id), cell = element('td'); cell.append(element('strong', comparisonValue(value.value, row.unit)));
        if (value.status !== 'ok') cell.append(element('small', ({diagnostic: '诊断数值', missing: '资料缺失', not_comparable: '不可直接比较'})[value.status] || value.status));
        if (list(value.reasons).length) cell.append(element('small', value.reasons.join('；')));
        const basis = value.basis || {}; if (basis.period || basis.scope) cell.append(element('small', [basis.period, ({cumulative: '累计', quarterly: '单季度', annual: '完整年度'})[basis.period_basis], ({consolidated: '合并口径', parent: '归母口径'})[basis.scope] || basis.scope, basis.currency === 'CNY' ? '人民币' : basis.currency].filter(Boolean).join(' · ')));
        comparisonRefs(cell, value.source_refs, security.security_id); tr.append(cell);
      }
      const note = element('td'); note.append(element('span', comparisonLabels[row.comparability], 'tag' + (row.comparability === 'comparable' ? ' accent' : ' warn')), element('p', row.reason || '按该行统一口径核对。')); tr.append(note); body.append(tr);
    }
    card.append(wrap); sections.append(card);
  }
  const limitations = byId('comparison-limitations'); limitations.replaceChildren();
  for (const reason of list(data.limitations)) limitations.append(element('li', reason));
}

function renderData() {
  renderLocalData();
  updateFirstResearch();
  renderSummary();
  renderWatchlist(byId('overview-watchlist'));
  renderWatchlist(byId('research-watchlist'));
  renderModules();
  renderPending();
  renderReviews();
  renderArchive();
  renderTechnicalPanel();
  renderProjections();
  renderTasks();
  renderComparison();
  updateDraftAvailability();
}

async function initialize() {
  const generation = ++state.generation;
  invalidateComparison();
  if (!byId('comparison-asof').value) byId('comparison-asof').value = new Date().toLocaleDateString('sv-SE', {timeZone: 'Asia/Shanghai'});
  ++tasksReadVersion; clearTimeout(taskPollTimer);
  clearTimeout(localDataPollTimer); state.localData.clear();
  state.contexts.clear();
  state.technical.clear();
  state.fundamental.clear();
  state.analyst.clear();
  state.continuity.clear();
  chartView = null;
  byId('technical-chart').replaceChildren();
  byId('chart-readout').replaceChildren();
  byId('technical-provenance').replaceChildren();
  byId('technical-dashboard').hidden = true;
  byId('refresh-data').disabled = true;
  connection('正在读取本地档案…');
  try {
    const data = await api('/api/bootstrap');
    if (generation !== state.generation) return;
    state.bootstrap = data;
    state.watchlist = list(data.watchlist);
    state.securities.clear();
    remember(data.securities);
    remember(state.watchlist);
    const restored = viewPreferencesLoaded ? null : restoreViewPreferences();
    viewPreferencesLoaded = true;
    byId('sidebar-version').textContent = (data.version ? 'v' + data.version : '本地服务') + (data.phase ? ' · ' + data.phase : '');
    byId('phase-badge').textContent = (data.phase ? data.phase + ' · ' : '') + '本地研究';
    byId('tasks-phase').textContent = data.phase || '本地研究工作台';
    const previous = state.selected;
    const selected = restored?.security || (previous && state.securities.get(previous.security_id)) || previous || state.watchlist[0] || [...state.securities.values()][0];
    renderSettings();
    if (selected) selectSecurity(selected, false);
    else {
      state.selected = null;
      updateIdentity();
      renderData();
    }
    if (restored) state.ma = new Set(restored.ma);
    showPage(state.page, false);
    saveViewPreferences();
    connection('已载入已有研究。打开看板不刷新行情、不调用模型；研究任务仅在明确开始后运行。');
    refreshTasks();
    // Keep a large watchlist from issuing every context request at once.
    for (let index = 0; index < state.watchlist.length; index += 4) {
      if (generation !== state.generation) return;
      await Promise.all(state.watchlist.slice(index, index + 4).flatMap(security => [ensureContext(security), ensureTechnical(security, state.homePeriod, 60), ensureProjection(security, 'fundamental'), ensureProjection(security, 'analyst'), ensureProjection(security, 'continuity')]));
    }
  } catch (error) {
    if (generation !== state.generation) return;
    state.bootstrap = null;
    updateIdentity();
    renderData();
    renderSettings();
    connection(error.message, true, initialize);
    announce('连接失败。' + error.message);
  } finally {
    if (generation === state.generation) byId('refresh-data').disabled = false;
  }
}

async function toggleWatchlist() {
  const selected = state.selected;
  if (!selected || state.saving || !state.bootstrap?.csrf_token) return;
  const tracked = !isTracked(selected.security_id);
  state.saving = true;
  updateIdentity();
  try {
    const data = await api('/api/watchlist', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': state.bootstrap.csrf_token}, body: JSON.stringify({security_id: selected.security_id, tracked})});
    state.watchlist = list(data.securities);
    remember(state.watchlist);
    renderData();
    announce(securityLabel(selected) + (tracked ? '已加入关注，名单已保存。' : '已移出关注，历史档案保留。'));
  } catch (error) {
    connection('关注名单保存失败：' + error.message, true);
    announce('保存失败，名单未修改。' + error.message);
  } finally {
    state.saving = false;
    updateIdentity();
    byId('toggle-watchlist').focus({preventScroll: true});
  }
}

function closeSearch() {
  clearTimeout(searchTimer);
  searchRequest++;
  searchController?.abort();
  byId('search-results').hidden = true;
  byId('search-results').replaceChildren();
}

async function searchSecurities() {
  clearTimeout(searchTimer);
  const request = ++searchRequest;
  searchController?.abort();
  searchController = new AbortController();
  const query = byId('security-query').value.trim();
  const container = byId('search-results');
  container.hidden = false;
  if (!query) {
    empty(container, '先输入公司名称或证券代码', '从可信候选确认完整身份，再准备首次或延续研究。不会根据裸代码创建未知公司。');
    if (state.selected) container.append(button('为当前' + securityLabel(state.selected) + '准备研究草稿', () => prepareDraft('joint')));
    return;
  }
  empty(container, '正在查找…');
  try {
    const data = await api('/api/securities?q=' + encodeURIComponent(query), {signal: searchController.signal});
    if (request !== searchRequest) return;
    const candidates = list(data.securities);
    remember(candidates);
    if (!candidates.length) {
      empty(container, '未找到可信证券身份', '请核对名称、代码、市场或补充可信身份来源。输入已保留，不会据此创建未知公司。');
      container.append(button('继续核对证券身份', () => byId('security-query').focus()));
      announce('没有找到匹配的证券。');
      return;
    }
    container.replaceChildren();
    for (const security of candidates) {
      const control = button(securityLabel(security), () => selectSecurity(security), 'candidate');
      control.append(element('small', securityIdentity(security)));
      if (security.name_note) control.append(element('small', security.name_note));
      control.append(element('small', [security.has_archive === true || Number(security.result_count) > 0 ? '已有研究档案' : security.has_archive === false || security.result_count === 0 ? '尚无研究档案' : '归档状态将在选择后核对', ({archive: '档案', watchlist: '关注名单', local_csv_header: '可信本地证券索引'})[security.identity_source] || '本地证券身份'].join(' · ')));
      control.setAttribute('aria-label', '选择' + securityLabel(security) + '，' + securityIdentity(security));
      container.append(control);
    }
    announce('找到' + candidates.length + '只证券。请点击候选，或按向下键移动后确认。');
  } catch (error) {
    if (error.name === 'AbortError' || request !== searchRequest) return;
    empty(container, '证券查找失败', error.message, searchSecurities);
    announce('证券查找失败。' + error.message);
  }
}

document.addEventListener('click', event => {
  const control = event.target.closest('[data-page]');
  if (control) showPage(control.dataset.page);
  const prepare = event.target.closest('[data-prepare]');
  if (prepare) prepareDraft(prepare.dataset.prepare);
  if (!event.target.closest('#security-search')) closeSearch();
});
byId('security-search').addEventListener('submit', event => { event.preventDefault(); searchSecurities(); });
byId('comparison-add-current').addEventListener('click', () => addComparison(state.selected));
byId('comparison-add-candidate').addEventListener('click', () => addComparison(state.securities.get(byId('comparison-candidate').value)));
byId('comparison-candidate').addEventListener('change', () => { byId('comparison-add-candidate').disabled = !byId('comparison-candidate').value || state.comparison.pool.length >= 5; });
byId('comparison-form').addEventListener('submit', event => { event.preventDefault(); loadComparison(); });
for (const id of ['comparison-asof', 'comparison-basis', 'comparison-period', 'comparison-window']) byId(id).addEventListener('change', () => invalidateComparison(id === 'comparison-asof' || id === 'comparison-basis'));
byId('security-query').addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchRequest++;
  searchController?.abort();
  if (!byId('security-query').value.trim()) { closeSearch(); return; }
  searchTimer = setTimeout(searchSecurities, 250);
});
byId('security-search').addEventListener('keydown', event => {
  if (event.key === 'Escape') { closeSearch(); byId('security-query').focus(); return; }
  if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return;
  const candidates = [...byId('search-results').querySelectorAll('button')];
  if (!candidates.length || byId('search-results').hidden) return;
  event.preventDefault();
  const position = candidates.indexOf(document.activeElement);
  const next = event.key === 'ArrowDown' ? (position + 1) % candidates.length : position <= 0 ? candidates.length - 1 : position - 1;
  candidates[next].focus();
});
byId('toggle-watchlist').addEventListener('click', toggleWatchlist);
byId('refresh-data').addEventListener('click', initialize);
byId('local-data-check').addEventListener('click', () => operateLocalData('preview'));
byId('new-security-button').addEventListener('click', () => {
  byId('security-query').focus();
  searchSecurities();
  announce('先检索并确认完整证券身份。选择无档证券后，可准备首次研究草稿。');
});
byId('analyst-refresh').addEventListener('click', refreshAnalyst);
byId('analyst-dialog-close').addEventListener('click', () => byId('analyst-dialog').close());
byId('continuity-refresh').addEventListener('click', () => {
  if (state.selected) ensureProjection(state.selected, 'continuity', true);
});
byId('continuity-review-cancel').addEventListener('click', () => byId('continuity-review-dialog').close());
byId('continuity-review-dialog').addEventListener('close', () => { continuityReviewDraft = null; });
byId('continuity-review-form').addEventListener('submit', saveContinuityReview);
for (const id of ['continuity-status', 'continuity-module', 'continuity-query']) byId(id).addEventListener(id === 'continuity-query' ? 'input' : 'change', () => {
  const data = state.selected && state.continuity.get(state.selected.security_id)?.data;
  if (data) renderContinuityItems(data);
});
byId('continuity-filters').addEventListener('submit', event => event.preventDefault());
byId('continuity-filters').addEventListener('reset', () => {
  for (const id of ['continuity-status', 'continuity-module', 'continuity-query']) byId(id).value = '';
  const data = state.selected && state.continuity.get(state.selected.security_id)?.data;
  if (data) renderContinuityItems(data);
});
document.querySelectorAll('[data-fin-period]').forEach(control => control.addEventListener('click', () => {
  state.finPeriod = control.dataset.finPeriod;
  const data = state.selected && state.fundamental.get(state.selected.security_id)?.data;
  if (data) renderFinancialCharts(data);
  saveViewPreferences();
}));
document.querySelectorAll('[data-tech-period]').forEach(control => control.addEventListener('click', () => setTechnicalPeriod(control.dataset.techPeriod)));
document.querySelectorAll('[data-home-period]').forEach(control => control.addEventListener('click', () => {
  state.homePeriod = control.dataset.homePeriod;
  document.querySelectorAll('[data-home-period]').forEach(node => node.setAttribute('aria-pressed', String(node.dataset.homePeriod === state.homePeriod)));
  renderWatchlist(byId('overview-watchlist'));
  renderWatchlist(byId('research-watchlist'));
  state.watchlist.forEach(security => ensureTechnical(security, state.homePeriod, 60));
  saveViewPreferences();
}));
document.querySelectorAll('[data-indicator]').forEach(control => control.addEventListener('click', () => {
  state.indicator = control.dataset.indicator;
  document.querySelectorAll('[data-indicator]').forEach(node => node.setAttribute('aria-pressed', String(node.dataset.indicator === state.indicator)));
  const data = technicalEntry()?.data;
  if (data) drawTechnicalChart(data);
  saveViewPreferences();
}));
document.querySelectorAll('[data-window]').forEach(control => control.addEventListener('click', () => changeWindow(control.dataset.window)));
byId('chart-position').addEventListener('input', () => {
  state.chartStart = Number(byId('chart-position').value);
  const data = technicalEntry()?.data;
  if (data) drawTechnicalChart(data);
});
for (const [id, direction] of [['chart-older', -1], ['chart-newer', 1]]) byId(id).addEventListener('click', () => {
  if (!chartView) return;
  state.chartStart = chartView.start + direction * Math.max(1, Math.floor(chartView.bars.length / 2));
  drawTechnicalChart(chartView.data);
});
byId('technical-chart').addEventListener('pointermove', event => {
  if (!chartView) return;
  const rect = byId('technical-chart').getBoundingClientRect();
  const x = (event.clientX - rect.left) / rect.width * 1000;
  const index = Math.floor((x - chartView.left) / chartView.width * chartView.bars.length);
  updateChartHover(index);
});
byId('technical-chart').addEventListener('pointerleave', () => { if (chartView) updateChartHover(chartView.bars.length - 1, false); });
byId('technical-chart').addEventListener('keydown', event => {
  if (!chartView || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
  event.preventDefault();
  const index = event.key === 'Home' ? 0 : event.key === 'End' ? chartView.bars.length - 1 : chartView.hoverIndex + (event.key === 'ArrowRight' ? 1 : -1);
  updateChartHover(index);
  announce(byId('chart-readout').textContent);
});
byId('technical-chart').addEventListener('wheel', event => {
  if (!chartView || !event.ctrlKey) return;
  event.preventDefault();
  const total = list(chartView.data.bars).length;
  const count = Math.min(total, Math.max(10, Math.round(chartView.bars.length * (event.deltaY > 0 ? 1.25 : .8))));
  const center = chartView.start + chartView.hoverIndex;
  state.chartWindow = count;
  state.chartStart = Math.round(center - count / 2);
  drawTechnicalChart(chartView.data);
  saveViewPreferences();
}, {passive: false});
byId('draft-form').addEventListener('submit', saveDraft);
byId('draft-start').addEventListener('click', startResearch);
byId('draft-mode').addEventListener('change', () => { renderDraftSources(); updateDraftAvailability(); });
byId('draft-module').addEventListener('change', () => { if (byId('draft-module').value === 'technical') draftSourceIds = []; renderDraftSources(); updateDraftAvailability(); });
byId('draft-provider').addEventListener('change', updateDraftAvailability);
byId('tasks-refresh').addEventListener('click', () => refreshTasks());
byId('tasks-filter').addEventListener('change', renderTasks);
byId('draft-cancel').addEventListener('click', () => byId('draft-dialog').close());
byId('draft-dialog').addEventListener('close', () => {
  state.draftSecurity = null;
  if (draftTrigger?.isConnected) draftTrigger.focus();
});
byId('archive-filters').addEventListener('submit', event => event.preventDefault());
byId('archive-filters').addEventListener('change', renderArchive);
byId('archive-query').addEventListener('input', renderArchive);
byId('archive-filters').addEventListener('reset', () => { setTimeout(renderArchive, 0); });
byId('reader-close').addEventListener('click', () => byId('reader-dialog').close());
byId('reader-dialog').addEventListener('close', () => {
  clearTimeout(readerTimer);
  byId('report-frame').removeAttribute('src');
  if (readerTrigger?.isConnected) readerTrigger.focus();
});
byId('report-frame').addEventListener('load', () => {
  if (!byId('reader-dialog').open || !byId('report-frame').hasAttribute('src')) return;
  clearTimeout(readerTimer);
  byId('reader-status').textContent = '阅读页已载入，附件可从上方单独打开。若正文提示错误，请关闭后重新读取档案。';
});

renderData();
initialize();
