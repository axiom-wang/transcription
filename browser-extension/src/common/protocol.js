// @ts-check
/**
 * 三个上下文（service worker / offscreen / panel）之间的消息契约。
 *
 * ## 关键约定：显式寻址
 *
 * `chrome.runtime.sendMessage` 会**广播到所有扩展上下文**。所以每条消息都带 `to` 字段，
 * 每个监听器在 `to` 不匹配时必须**严格 `return false`**。
 *
 * ⚠️ 在非目标监听器里 `return true` 会破坏 `sendResponse` 路由 —— 这是本模式最常见的 bug：
 * `return true` 表示「我会异步回复」，于是 Chrome 会一直等这个永不到来的回复，
 * 而真正的目标监听器的回复反而被丢弃。务必只在确实要异步回复时 return true。
 *
 * ## 为什么不用长连接 Port
 *
 * offscreen→SW 的 Port 会无限期钉住 service worker，违背 MV3 的设计意图。
 * 请求/响应式 sendMessage 对 SW 死亡天然免疫，且不需要任何保活技巧。
 *
 * ## 为什么 UI 不接收推送
 *
 * panel 读 `chrome.storage.session` 并订阅 `onChanged`，而不是接收 SW 推送。
 * 这消除了「panel 关闭时 Receiving end does not exist」这一整类错误，
 * 并免费获得 SW 死亡后的状态重同步。推论：panel 用 `startedAt` **本地**计算已录时长，
 * 无需 1Hz 消息流量。
 */

/** 消息信封版本，便于日后演进 */
export const PROTOCOL_VERSION = 1;

/** @typedef {'sw'|'offscreen'} Target */

/**
 * @typedef {object} Envelope
 * @property {number} v 协议版本
 * @property {Target} to 收件方 —— 不匹配的监听器必须 return false
 * @property {string} type 消息类型
 */

// ───────────────────────────── UI → SW ─────────────────────────────

export const UI = {
  SEND_TO_TINGJI: 'UI_SEND_TO_TINGJI',
  OPEN_TINGJI: 'UI_OPEN_TINGJI',
  PING_TINGJI: 'UI_PING_TINGJI',
  /** 取当前完整状态快照 */
  GET_SNAPSHOT: 'UI_GET_SNAPSHOT',
  /** 列出候选 Tab（audible ∪ armed ∪ capturing） */
  LIST_CANDIDATES: 'UI_LIST_CANDIDATES',
  /** 开始录制指定 Tab */
  START: 'UI_START',
  /** 开始录制全部已授权 Tab */
  START_ALL: 'UI_START_ALL',
  /** 停止指定 Tab */
  STOP: 'UI_STOP',
  /** 停止全部 */
  STOP_ALL: 'UI_STOP_ALL',
  /** 从录制列表移除（放弃授权信念） */
  DISARM: 'UI_DISARM',
  /** 重试失败的 Tab */
  RETRY: 'UI_RETRY',
  /** 切换某 Tab 的回放监听 */
  SET_MONITOR: 'UI_SET_MONITOR',
  /** 全局回放开关：一次改掉所有录制中的 Tab，并记住这个选择 */
  SET_MONITOR_ALL: 'UI_SET_MONITOR_ALL',
  /** 读取偏好设置（panel 初始化时需要知道全局回放开关的当前值） */
  GET_PREFS: 'UI_GET_PREFS',
  /** 跳转到某 Tab（让用户去按快捷键授权） */
  ACTIVATE_TAB: 'UI_ACTIVATE_TAB',
  /** 修改偏好设置 */
  SET_PREF: 'UI_SET_PREF',
  /** 保存 IDB 中恢复出的未保存录音 */
  RECOVER_SAVE: 'UI_RECOVER_SAVE',
  /** 丢弃 IDB 中恢复出的未保存录音 */
  RECOVER_DISCARD: 'UI_RECOVER_DISCARD',
  /** 调试用：探测某 Tab 的授权是否仍然有效 */
  DEBUG_PROBE_GRANT: 'UI_DEBUG_PROBE_GRANT',
};

// ─────────────────────────── SW → Offscreen ───────────────────────────

export const OFF = {
  /**
   * 健康检查 + 状态对账。
   * 回复 `{ok, version, tabs: OffscreenTabState[]}`。
   * offscreen 是持久状态所有者，**它的回复赢得所有冲突**。
   */
  PING: 'OFFSCREEN_PING',
  /**
   * 启动单个 Tab 的录制。
   *
   * 回复 `{ok:true}` 的含义是**兑换已完成** —— getUserMedia 已返回且
   * MediaRecorder.start() 已调用。这正是顺序铸造循环等待的那个 ack：
   * streamId 单次使用且只有 10 秒 TTL，必须确认兑换后才能铸下一个。
   */
  START_ONE: 'OFFSCREEN_START_ONE',
  /** 停止单个 Tab */
  STOP_ONE: 'OFFSCREEN_STOP_ONE',
  /** 停止全部 */
  STOP_ALL: 'OFFSCREEN_STOP_ALL',
  /** 切换回放监听 */
  SET_MONITOR: 'OFFSCREEN_SET_MONITOR',
  /** 批量切换回放监听（全局静音开关） */
  SET_MONITOR_ALL: 'OFFSCREEN_SET_MONITOR_ALL',
  /** 丢弃某 Tab 已录数据，不保存 */
  DISCARD: 'OFFSCREEN_DISCARD',
  /** 列出 IDB 中可恢复的会话 */
  LIST_RECOVERABLE: 'OFFSCREEN_LIST_RECOVERABLE',
  /** 保存 IDB 中某个可恢复会话 */
  RECOVER_SAVE: 'OFFSCREEN_RECOVER_SAVE',
  /** 丢弃 IDB 中某个可恢复会话 */
  RECOVER_DISCARD: 'OFFSCREEN_RECOVER_DISCARD',
  /**
   * 回收一个 blob URL。
   *
   * 由 SW 在下载到达终态（complete / interrupted）后发出。
   * blob URL 必须在**创建它的上下文**里回收，而创建方是 offscreen 文档
   * （service worker 里没有 URL.createObjectURL）。
   */
  REVOKE_URL: 'OFFSCREEN_REVOKE_URL',
};

// ─────────────────────── Offscreen → SW（主动上报） ───────────────────────

export const EV = {
  /** 某 Tab 状态变化 `{tabId, state, at, bytes?, durationMs?, error?, audio?}` */
  TAB_STATE: 'SW_EVENT_TAB_STATE',
  /** 文件已保存 `{tabId, filename, bytes, durationMs}` */
  SAVED: 'SW_EVENT_SAVED',
  /** IDB 里发现未保存的录音 `{sessions}` */
  RECOVERABLE: 'SW_EVENT_RECOVERABLE',
  /**
   * 请求 SW 执行下载 `{blobUrl, filename, fallbackFilename, tabId, bytes}`。
   *
   * ## 为什么必须绕这一圈
   *
   * `chrome.downloads` 在 offscreen 文档里**不存在** —— 官方文档明确写了两次
   * 「runtime API 是 offscreen 文档唯一支持的扩展 API」。而 `URL.createObjectURL`
   * 在 service worker 里**也不存在**（ServiceWorkerGlobalScope 没有它）。
   *
   * 两个约束一夹，唯一可行的分工就是：
   *   offscreen 造 blob URL → 消息传给 SW → SW 调 downloads.download
   *   → 下载终态后 SW 发 REVOKE_URL 回来 → offscreen 回收
   *
   * 注意 blob URL 必须在 offscreen 保持**未回收**直到下载结束，
   * 提前回收会截断文件。
   */
  REQUEST_DOWNLOAD: 'SW_EVENT_REQUEST_DOWNLOAD',
};

// ───────────────────────────── 类型定义 ─────────────────────────────

/**
 * @typedef {object} TabMeta
 * @property {string} title Tab 标题（用于文件名）
 * @property {string} url Tab URL（用于显示来源和判断同源）
 * @property {number} index 授权顺序，决定文件名前缀序号
 */

/**
 * @typedef {object} RecordOpts
 * @property {number} timesliceMs MediaRecorder timeslice；必须 > 0 以支持分块落盘
 * @property {number} [audioBitsPerSecond] Opus 码率
 * @property {boolean} monitor 是否把音频回放给用户
 * @property {string} sessionId 本次「开始录制」会话的 id，用于分组文件夹和 IDB 键
 * @property {string} docType EBML DocType 目标值，通常是 'matroska'
 * @property {string} extension 输出扩展名，通常是 'mkv'
 */

/**
 * @typedef {object} OffscreenTabState
 * @property {number} tabId
 * @property {import('./states.js').TabState} state
 * @property {number} startedAt
 * @property {number} bytes
 * @property {number} chunks
 * @property {boolean} monitor
 * @property {import('./audio-level.js').AudioSnapshot} [audio]
 */

/**
 * @typedef {object} TabRecord SW 侧的每 Tab 记录，会镜像进 chrome.storage.session
 * @property {number} tabId
 * @property {import('./states.js').TabState} state
 * @property {string} title
 * @property {string} url
 * @property {string} origin 授权时的 origin；跨源跳转即判定授权失效
 * @property {number} index 授权顺序
 * @property {number} [startedAt] 录制开始时间戳，panel 据此本地计算时长
 * @property {number} [bytes]
 * @property {number} [durationMs]
 * @property {boolean} [monitor]
 * @property {import('./audio-level.js').AudioSnapshot} [audio] 音频活性心跳（录制中约 2 秒一次）
 * @property {number} [downloadId]
 * @property {boolean} [downloadComplete]
 * @property {string} [downloadBlobUrl]
 * @property {'sending'|'queued'|'reused'|'error'} [tingji]
 * @property {string} [tingjiMessage]
 * @property {string} [filename] 保存后的文件名
 * @property {{code:string, message:string}} [error]
 * @property {boolean} [audible] 最近一次已知的出声状态，仅用于排序和发现
 * @property {string} [favIconUrl]
 */

/**
 * 构造一条发往指定上下文的消息。
 * @template {object} T
 * @param {Target} to
 * @param {string} type
 * @param {T} [payload]
 * @returns {Envelope & T}
 */
export function msg(to, type, payload) {
  return /** @type {any} */ ({ v: PROTOCOL_VERSION, to, type, ...(payload ?? {}) });
}

/**
 * 判断消息是否发给本上下文。监听器的第一行就应该用它，不匹配立刻 return false。
 * @param {unknown} message
 * @param {Target} self
 * @returns {boolean}
 */
export function isFor(message, self) {
  return (
    typeof message === 'object' && message !== null &&
    /** @type {any} */ (message).to === self &&
    typeof /** @type {any} */ (message).type === 'string'
  );
}

/**
 * 给 Promise 加超时。用于顺序铸造循环 —— streamId 的 TTL 是 10 秒，
 * 我们用 8 秒超时，这样超时几乎必然意味着「已过期」而不是「还在路上」。
 * @template T
 * @param {Promise<T>} promise
 * @param {number} ms
 * @param {string} [label]
 * @returns {Promise<T>}
 */
export function withTimeout(promise, ms, label = '操作') {
  /** @type {ReturnType<typeof setTimeout>} */
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label}超时（${ms}ms）`)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

/**
 * 向另一个上下文发消息并等回复。
 *
 * 注意：这里刻意**不**吞掉错误。「Receiving end does not exist」是有意义的信号
 * （通常表示 offscreen 文档不存在或已崩溃），调用方需要看到它以便重建。
 *
 * @param {Target} to
 * @param {string} type
 * @param {object} [payload]
 * @returns {Promise<any>}
 */
export async function sendTo(to, type, payload) {
  return chrome.runtime.sendMessage(msg(to, type, payload));
}
