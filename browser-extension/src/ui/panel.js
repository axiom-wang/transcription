// @ts-check
/**
 * 侧边栏逻辑。
 *
 * ## 核心设计：panel 只读 storage，不接收推送
 *
 * panel 读 `chrome.storage.session` 并订阅 `onChanged`，**从不**作为消息接收方。
 * 这消除了「panel 关闭时 SW 推送报 Receiving end does not exist」这一整类错误，
 * 并且免费获得 SW 死亡后的状态重同步（SW 重启会重写 storage，panel 自动跟上）。
 *
 * 推论：**已录时长在本地根据 startedAt 计算**，不需要 1Hz 的消息流量。
 * 字节数由 offscreen 以 2 秒心跳更新，够用了。
 */

import { T, stateLabel, shortcutLabel } from './strings.js';
import { UI, sendTo } from '../common/protocol.js';
import { formatBytes, formatDuration, createLogger } from '../common/log.js';
import { S } from '../common/states.js';
import { NO_SOUND_WARN_MS, SILENT_INDICATOR_MS } from '../common/audio-level.js';

const log = createLogger('panel');

const SHORTCUT = shortcutLabel();

/** 候选列表刷新的防抖间隔。audible 有约 2 秒防抖且会抖动，跟太紧只会让列表乱跳。 */
const REFRESH_DEBOUNCE_MS = 2500;

const el = {
  autoTingji: /** @type {HTMLInputElement} */ (document.getElementById('autoTingji')),
  connectorStatus: /** @type {HTMLElement} */ (document.getElementById('connectorStatus')),
  openTingji: /** @type {HTMLButtonElement} */ (document.getElementById('openTingji')),
  summary: /** @type {HTMLElement} */ (document.getElementById('summary')),
  statusDot: /** @type {HTMLElement} */ (document.getElementById('statusDot')),
  startAll: /** @type {HTMLButtonElement} */ (document.getElementById('startAll')),
  stopAll: /** @type {HTMLButtonElement} */ (document.getElementById('stopAll')),
  list: /** @type {HTMLElement} */ (document.getElementById('list')),
  empty: /** @type {HTMLElement} */ (document.getElementById('empty')),
  explainer: /** @type {HTMLElement} */ (document.getElementById('explainer')),
  explainerTitle: /** @type {HTMLElement} */ (document.getElementById('explainerTitle')),
  explainerBody: /** @type {HTMLElement} */ (document.getElementById('explainerBody')),
  explainerClose: /** @type {HTMLButtonElement} */ (document.getElementById('explainerClose')),
  recoverBanner: /** @type {HTMLElement} */ (document.getElementById('recoverBanner')),
  recoverTitle: /** @type {HTMLElement} */ (document.getElementById('recoverTitle')),
  recoverBody: /** @type {HTMLElement} */ (document.getElementById('recoverBody')),
  recoverSaveAll: /** @type {HTMLButtonElement} */ (document.getElementById('recoverSaveAll')),
  recoverDiscardAll: /** @type {HTMLButtonElement} */ (document.getElementById('recoverDiscardAll')),
  warnMany: /** @type {HTMLElement} */ (document.getElementById('warnMany')),
  monitorAll: /** @type {HTMLInputElement} */ (document.getElementById('monitorAll')),
  monitorLabel: /** @type {HTMLElement} */ (document.getElementById('monitorLabel')),
  monitorHint: /** @type {HTMLElement} */ (document.getElementById('monitorHint')),
  shortcutHint: /** @type {HTMLElement} */ (document.getElementById('shortcutHint')),
};

/** @type {any[]} */
let rows = [];
/** @type {any[]} */
let recoverable = [];
/** 全局回放偏好。初始值由 GET_PREFS 填入，之后由开关驱动。 */
let monitorAll = false;
/** 正在进行的操作（tabId 或 'all'），用于禁用按钮防重复点击 */
/** @type {Set<string>} */
const busy = new Set();

// ───────────────────────────── 初始化 ─────────────────────────────

function initStaticText() {
  el.explainerTitle.textContent = T.explainerTitle;
  el.explainerBody.textContent = T.explainerBody;
  el.explainerClose.textContent = T.explainerDismiss;
  el.recoverSaveAll.textContent = T.recoverSaveAll;
  el.recoverDiscardAll.textContent = T.recoverDiscardAll;
  el.warnMany.textContent = T.warnManyTabs;
  el.monitorLabel.textContent = T.monitorGlobalLabel;
  /** @type {HTMLElement} */ (el.empty.querySelector('.empty-title')).textContent = T.emptyTitle;
  /** @type {HTMLElement} */ (el.empty.querySelector('.empty-hint')).textContent = T.emptyHint;
  el.shortcutHint.innerHTML =
    `授权当前标签页：<kbd>${SHORTCUT}</kbd> · 开始/停止全部：<kbd>${
      SHORTCUT.replace(/R$/, 'S')}</kbd>`;
}

/** 渲染全局回放开关的状态与提示语。 */
function renderMonitorBar() {
  el.monitorAll.checked = monitorAll;
  el.monitorHint.textContent = monitorAll
    ? T.monitorGlobalHintOn
    : T.monitorGlobalHintMuted;
  el.monitorHint.classList.toggle('is-muted', !monitorAll);
}

// ───────────────────────────── 渲染 ─────────────────────────────

/** @param {string} state */
function chipClass(state) {
  switch (state) {
    case S.ARMED: return 'chip-armed';
    case S.RECORDING: return 'chip-recording';
    case S.STARTING:
    case S.STOPPING:
    case S.FINALIZING:
    case S.SAVING: return 'chip-busy';
    case S.DONE: return 'chip-done';
    case S.ERROR: return 'chip-error';
    default: return 'chip-idle';
  }
}

/** @param {string} state */
function rowClass(state) {
  switch (state) {
    case S.RECORDING:
    case S.STARTING:
    case S.STOPPING:
    case S.FINALIZING:
    case S.SAVING: return 'is-recording';
    case S.ARMED: return 'is-armed';
    case S.DONE: return 'is-done';
    case S.ERROR: return 'is-error';
    default: return '';
  }
}

/** @param {string} url */
function displayOrigin(url) {
  try {
    return new URL(url).host;
  } catch {
    return url || '';
  }
}

function render() {
  renderRecoverBanner();
  renderExplainer();
  renderMonitorBar();

  const recording = rows.filter((r) => isActiveState(r.state));
  const armed = rows.filter((r) => r.state === S.ARMED);
  // 与 SW 的 armedTabIds 一致：origin 为空表示授权已失效，「全部开始」不会选中它
  const startable = rows.filter(
    (r) => r.origin && (r.state === S.ARMED || r.state === S.DONE || r.state === S.ERROR),
  );

  // 汇总与状态点
  el.statusDot.className = 'dot';
  if (recording.length > 0) {
    el.summary.textContent = T.summaryRecording(recording.length);
    el.statusDot.classList.add('recording');
  } else if (armed.length > 0) {
    el.summary.textContent = T.summaryArmed(armed.length);
    el.statusDot.classList.add('armed');
  } else {
    el.summary.textContent = T.summaryNone;
  }

  // 顶栏按钮
  el.stopAll.hidden = recording.length === 0;
  el.stopAll.textContent = T.actionStopAll;
  el.stopAll.disabled = busy.has('all');

  el.startAll.hidden = recording.length > 0 || startable.length === 0;
  el.startAll.textContent = `${T.actionStartAll} (${startable.length})`;
  el.startAll.disabled = busy.has('all');

  el.warnMany.hidden = startable.length <= 8 && recording.length <= 8;

  // 行
  el.list.textContent = '';
  el.empty.hidden = rows.length > 0;

  for (const row of rows) {
    el.list.appendChild(renderRow(row));
  }
}

/** @param {string} state */
function isActiveState(state) {
  return [S.STARTING, S.RECORDING, S.STOPPING, S.FINALIZING, S.SAVING].includes(state);
}

/**
 * @param {any} row
 * @returns {HTMLLIElement}
 */
function renderRow(row) {
  const li = document.createElement('li');
  li.className = `row ${rowClass(row.state)}`;
  li.dataset.tabId = String(row.tabId);

  // 图标
  const icon = document.createElement('img');
  icon.className = 'row-icon';
  icon.alt = '';
  if (row.favIconUrl) icon.src = row.favIconUrl;
  // 图标加载失败很常见（很多站点的 favicon 会 404），静默隐藏即可
  icon.addEventListener('error', () => { icon.style.visibility = 'hidden'; });
  li.appendChild(icon);

  // 标题
  const title = document.createElement('div');
  title.className = 'row-title';
  title.textContent = row.title || `标签页 ${row.tabId}`;
  title.title = row.title || '';
  li.appendChild(title);

  // 状态 chip
  const chip = document.createElement('span');
  chip.className = `chip ${chipClass(row.state)}`;
  chip.textContent = stateLabel(row.state);
  li.appendChild(chip);

  // 第二行：错误信息优先，其次是状态相关的元信息
  if (row.state === S.ERROR && row.error) {
    const err = document.createElement('div');
    err.className = 'row-error';
    err.textContent = `${T.errPrefix}${row.error.message}`;
    li.appendChild(err);
  } else {
    const meta = document.createElement('div');
    meta.className = 'row-meta';
    meta.appendChild(buildMeta(row));
    li.appendChild(meta);
  }

  // 长时间完全无声的警告单独占一行 —— 它比 meta 重要，
  // 且长度会撑破 meta 的单行截断
  if (shouldWarnNoSound(row)) {
    const warn = document.createElement('div');
    warn.className = 'row-audio-warn';
    warn.textContent = T.audioNoSound;
    li.appendChild(warn);
  }

  // 操作区
  const acts = document.createElement('div');
  acts.className = 'row-acts';
  for (const btn of buildActions(row)) acts.appendChild(btn);
  li.appendChild(acts);

  return li;
}

/**
 * @param {any} row
 * @returns {DocumentFragment}
 */
function buildMeta(row) {
  const frag = document.createDocumentFragment();

  if (row.state === S.RECORDING && row.startedAt) {
    // ★ 时长在本地计算 —— 这就是 panel 不需要 1Hz 消息的原因
    const timer = document.createElement('span');
    timer.className = 'row-timer';
    timer.dataset.startedAt = String(row.startedAt);
    timer.textContent = formatDuration(Date.now() - row.startedAt);
    frag.appendChild(timer);

    if (row.bytes) {
      frag.appendChild(document.createTextNode(` · ${formatBytes(row.bytes)}`));
    }

    const audio = buildAudioIndicator(row);
    if (audio) frag.appendChild(audio);
    return frag;
  }

  if (row.state === S.DONE && row.filename) {
    const parts = [T.savedAs(row.filename.split('/').pop() ?? row.filename)];
    if (row.durationMs) parts.push(formatDuration(row.durationMs));
    frag.appendChild(document.createTextNode(parts.join(' · ')));
    return frag;
  }

  if (row.state === S.IDLE) {
    frag.appendChild(document.createTextNode(T.hintArmThisTab(SHORTCUT)));
    return frag;
  }

  frag.appendChild(document.createTextNode(displayOrigin(row.url)));
  return frag;
}

/**
 * 录制中的音频活性指示（「● 有声 / ○ 静音」）。
 *
 * 返回 null 表示不渲染 —— 数据缺失是正常情形（旧版 offscreen、
 * 分析链路因自动播放策略不可用），「没有指示」好过「错误的指示」。
 * @param {any} row
 * @returns {HTMLSpanElement | null}
 */
function buildAudioIndicator(row) {
  const a = row.audio;
  if (!a || typeof a.silentMs !== 'number') return null;

  const span = document.createElement('span');
  if (a.silentMs < SILENT_INDICATOR_MS) {
    span.className = 'row-audio active';
    span.textContent = ` · ${T.audioActive}`;
  } else {
    span.className = 'row-audio';
    span.textContent = ` · ${T.audioSilent}`;
  }
  return span;
}

/**
 * 是否该显示「未检测到声音」警告：录制开始后**从未**有声，
 * 且持续时长已超过阈值。有过声音再安静（演讲间隙）不警告 ——
 * 那是正常内容，警告只会制造焦虑。
 * @param {any} row
 */
function shouldWarnNoSound(row) {
  const a = row.audio;
  return (
    row.state === S.RECORDING &&
    a && typeof a.silentMs === 'number' &&
    !a.hasSound &&
    a.silentMs > NO_SOUND_WARN_MS
  );
}

/**
 * @param {any} row
 * @returns {HTMLElement[]}
 */
function buildActions(row) {
  const key = String(row.tabId);
  const isBusy = busy.has(key) || busy.has('all');
  /** @type {HTMLElement[]} */
  const out = [];

  /**
   * @param {string} label
   * @param {string} cls
   * @param {() => void | Promise<void>} onClick
   */
  const add = (label, cls, onClick) => {
    const b = document.createElement('button');
    b.className = `btn btn-small ${cls}`;
    b.textContent = label;
    b.disabled = isBusy;
    b.addEventListener('click', () => void onClick());
    out.push(b);
    return b;
  };

  switch (row.state) {
    case S.IDLE:
      // 未授权 —— 这正是用户学会「必须逐个授权」的地方。
      // 绝不假装成功：不自动授权，不静默失败，只提供一个「过去那边」的捷径。
      add(T.actionGoToTab, 'btn-ghost', () => activateTab(row.tabId));
      break;

    case S.ARMED:
      add(T.actionStart, 'btn-primary', () => startTabs([row.tabId]));
      add(T.actionRemove, 'btn-ghost', () => disarm(row.tabId));
      break;

    case S.RECORDING:
      add(T.actionStop, 'btn-danger', () => stopTabs([row.tabId]));
      add(
        row.monitor === false ? T.actionMonitorOff : T.actionMonitorOn,
        'btn-ghost',
        () => setMonitor(row.tabId, row.monitor === false),
      ).title = T.monitorTitle;
      break;

    case S.STARTING:
    case S.STOPPING:
    case S.FINALIZING:
    case S.SAVING:
      // 过渡态不给操作 —— 此时任何点击都只会制造竞态
      break;

    case S.DONE: {
      const sending = row.tingji === 'sending';
      const label = sending ? T.tingjiSending : row.tingji === 'error' ? T.tingjiRetry
        : row.tingji === 'queued' ? T.tingjiQueued : row.tingji === 'reused' ? T.tingjiReused
        : T.sendToTingji;
      add(label, 'btn-primary', () => withBusy(key, () => sendTo('sw', UI.SEND_TO_TINGJI, { tabId: row.tabId })))
        .disabled = isBusy || sending || row.downloadComplete === false || typeof row.downloadId !== 'number';
      add(T.openTingji, 'btn-link', openTingji);
      if (row.tingjiMessage) {
        const status = document.createElement('p');
        status.className = `tingji-status ${row.tingji === 'error' ? 'has-error' : ''}`;
        status.textContent = row.tingjiMessage;
        status.setAttribute('role', 'status');
        out.push(status);
      }
      // 授权已失效（跨源跳转或标签页已关闭）时不给「再次录制」：它只会失败
      if (row.origin) add(T.actionRecordAgain, '', () => startTabs([row.tabId]));
      add(T.actionRemove, 'btn-ghost', () => disarm(row.tabId));
      break;
    }

    case S.ERROR:
      // 授权已失效时不给「重试」—— 重试只会再失败一次。
      // 改为引导用户去那个标签页重新授权，这才是真正的解法。
      if (row.error?.code === 'E_GRANT_LOST' || !row.origin) {
        add(T.actionGoToTab, 'btn-ghost', () => activateTab(row.tabId));
      } else {
        add(T.actionRetry, 'btn-primary', () => startTabs([row.tabId]));
      }
      add(T.actionRemove, 'btn-ghost', () => disarm(row.tabId));
      break;
  }

  return out;
}

function renderExplainer() {
  const hidden = localStorage.getItem('explainerDismissed') === '1';
  // 用户已经授权过 ≥2 个标签页，说明这条约束已经学会了 —— 自动永久收起，别再啰嗦
  const learned = Number(localStorage.getItem('armCount') ?? '0') >= 2;
  el.explainer.hidden = hidden || learned;
}

function renderRecoverBanner() {
  if (recoverable.length === 0) {
    el.recoverBanner.hidden = true;
    return;
  }
  el.recoverBanner.hidden = false;
  el.recoverTitle.textContent = T.recoverTitle(recoverable.length);
  const total = recoverable.reduce((sum, s) => sum + (s.bytes || 0), 0);
  el.recoverBody.textContent = `${T.recoverBody}（共 ${formatBytes(total)}）`;
}

/**
 * 每秒只更新时长文本，不整体重绘。
 *
 * 整体重绘会重建所有按钮，导致鼠标悬停/焦点状态丢失 ——
 * 在一个用户会长时间盯着的界面里，那种闪烁很烦人。
 */
function tickTimers() {
  const now = Date.now();
  for (const node of el.list.querySelectorAll('.row-timer')) {
    const startedAt = Number(/** @type {HTMLElement} */ (node).dataset.startedAt);
    if (startedAt > 0) node.textContent = formatDuration(now - startedAt);
  }
}

// ───────────────────────────── 操作 ─────────────────────────────

/**
 * @template T
 * @param {string} key
 * @param {() => Promise<T>} fn
 */
async function withBusy(key, fn) {
  busy.add(key);
  render();
  try {
    return await fn();
  } finally {
    busy.delete(key);
    await refresh();
  }
}

/** @param {number[]} tabIds */
async function startTabs(tabIds) {
  const key = tabIds.length === 1 ? String(tabIds[0]) : 'all';
  await withBusy(key, async () => {
    const reply = await sendTo('sw', UI.START, { tabIds });
    logFailures(reply);
  });
}

async function startAll() {
  await withBusy('all', async () => {
    const reply = await sendTo('sw', UI.START_ALL);
    logFailures(reply);
  });
}

/** @param {any} reply */
function logFailures(reply) {
  const failed = (reply?.results ?? []).filter((/** @type {any} */ r) => !r.ok);
  if (failed.length > 0) {
    log.warn(`${failed.length} 个标签页启动失败`, failed);
  }
}

/** @param {number[]} tabIds */
async function stopTabs(tabIds) {
  const key = tabIds.length === 1 ? String(tabIds[0]) : 'all';
  await withBusy(key, () => sendTo('sw', UI.STOP, { tabIds }));
}

async function stopAll() {
  await withBusy('all', () => sendTo('sw', UI.STOP_ALL));
}

/** @param {number} tabId */
async function disarm(tabId) {
  await withBusy(String(tabId), () => sendTo('sw', UI.DISARM, { tabId }));
}

/** @param {number} tabId */
async function activateTab(tabId) {
  await sendTo('sw', UI.ACTIVATE_TAB, { tabId });
  // 刻意**不**关闭侧边栏 —— 这正是侧边栏相对 popup 的关键优势：
  // 用户切到那个标签页按快捷键时，面板还在，快捷键提示就在眼前。
}

/** @param {number} tabId @param {boolean} enabled */
async function setMonitor(tabId, enabled) {
  await sendTo('sw', UI.SET_MONITOR, { tabId, enabled });
}

/**
 * 全局回放开关。
 *
 * 立刻更新本地状态并重绘，不等服务端往返 —— 复选框的响应必须是即时的，
 * 否则会有「点了没动」的迟滞感。失败时回滚。
 * @param {boolean} enabled
 */
async function setMonitorAll(enabled) {
  const previous = monitorAll;
  monitorAll = enabled;
  renderMonitorBar();
  try {
    await sendTo('sw', UI.SET_MONITOR_ALL, { enabled });
  } catch (err) {
    log.warn('切换全局回放失败，已回滚', err);
    monitorAll = previous;
    renderMonitorBar();
  }
}

async function openTingji() {
  try {
    const result = await sendTo('sw', UI.OPEN_TINGJI);
    el.connectorStatus.textContent = result?.ok ? T.connectorReady : (result?.error || '无法打开听记');
  } catch {
    el.connectorStatus.textContent = '无法连接听记，请重新打开面板后重试';
  }
}

async function recoverAll(/** @type {boolean} */ save) {
  const items = [...recoverable];
  await withBusy('all', async () => {
    for (const item of items) {
      await sendTo('sw', save ? UI.RECOVER_SAVE : UI.RECOVER_DISCARD, {
        sessionId: item.sessionId, tabId: item.tabId,
      }).catch((err) => log.warn('恢复操作失败', err));
    }
    recoverable = [];
  });
}

// ───────────────────────────── 数据刷新 ─────────────────────────────

/** @type {ReturnType<typeof setTimeout> | null} */
let refreshTimer = null;

async function refresh() {
  try {
    const reply = await sendTo('sw', UI.LIST_CANDIDATES);
    if (reply?.ok) {
      rows = reply.tabs ?? [];
      // 统计已授权过的标签页数，用于自动收起说明条
      const armedEver = rows.filter((r) => r.state !== S.IDLE).length;
      const prev = Number(localStorage.getItem('armCount') ?? '0');
      if (armedEver > prev) localStorage.setItem('armCount', String(armedEver));
    }
    render();
  } catch (err) {
    log.warn('刷新失败', err);
  }
}

function scheduleRefresh() {
  if (refreshTimer !== null) return;
  refreshTimer = setTimeout(() => {
    refreshTimer = null;
    void refresh();
  }, REFRESH_DEBOUNCE_MS);
}

// ───────────────────────────── 接线 ─────────────────────────────

initStaticText();

el.autoTingji.addEventListener('change', () => {
  el.autoTingji.disabled = true;
  void sendTo('sw', UI.SET_PREF, { key: 'autoTingji', value: el.autoTingji.checked })
    .catch(() => { el.autoTingji.checked = !el.autoTingji.checked; })
    .finally(() => { el.autoTingji.disabled = false; });
});
el.openTingji.addEventListener('click', () => void openTingji());

el.startAll.addEventListener('click', () => void startAll());
el.stopAll.addEventListener('click', () => void stopAll());
el.recoverSaveAll.addEventListener('click', () => void recoverAll(true));
el.recoverDiscardAll.addEventListener('click', () => void recoverAll(false));
el.explainerClose.addEventListener('click', () => {
  localStorage.setItem('explainerDismissed', '1');
  renderExplainer();
});
el.monitorAll.addEventListener('change', () => {
  void setMonitorAll(el.monitorAll.checked);
});

// ★ 唯一的状态来源：storage.session 的变化。不接收任何推送。
chrome.storage.session.onChanged.addListener((changes) => {
  if (changes.tabRecords) {
    void refresh();
  }
});

// audible 变化会影响候选列表，但它抖动厉害，所以走防抖
chrome.tabs.onUpdated.addListener((_id, changeInfo) => {
  if (typeof changeInfo.audible === 'boolean' || changeInfo.title) scheduleRefresh();
});
chrome.tabs.onRemoved.addListener(() => scheduleRefresh());

setInterval(tickTimers, 1000);

void (async () => {
  // 先读偏好再首绘 —— 否则开关会先渲染成默认值、再跳到真实值，肉眼可见地闪一下
  try {
    const prefs = await sendTo('sw', UI.GET_PREFS);
    if (typeof prefs?.prefs?.monitor === 'boolean') {
      monitorAll = prefs.prefs.monitor;
    }
  } catch { /* SW 可能还在启动，用默认值先绘 */ }

  el.autoTingji.checked = (await chrome.storage.local.get('autoTingji')).autoTingji === true;
  try {
    const connector = await sendTo('sw', UI.PING_TINGJI);
    el.connectorStatus.textContent = connector?.ok ? T.connectorReady : connector?.error;
  } catch {
    el.connectorStatus.textContent = '听记连接器检测失败，请重新打开面板后重试';
  }
  await refresh();

  // 查询可恢复的录音（IDB 里有分块但未保存的）
  try {
    const reply = await sendTo('sw', UI.GET_SNAPSHOT);
    if (reply?.snapshot?.recoverable) {
      recoverable = reply.snapshot.recoverable;
      render();
    }
  } catch { /* SW 可能还在启动 */ }
})();

log.info('panel 已就绪');
