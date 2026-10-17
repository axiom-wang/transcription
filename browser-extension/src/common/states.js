// @ts-check
/**
 * 每 Tab 状态机：状态枚举 + 合法转移表。
 *
 * 转移表刻意以**数据**形式导出，而不是写成一堆 if/switch —— 这样单测能穷尽断言
 * 「代码实际发出的每个转移都在表里」，无需打开浏览器就能捕获状态 bug。
 * 详见 tests/states.test.js。
 */

/** @typedef {'idle'|'armed'|'starting'|'recording'|'stopping'|'finalizing'|'saving'|'done'|'error'} TabState */

/** @type {Record<string, TabState>} */
export const S = {
  /** 已知的 Tab（正在出声，或曾经见过），但**没有**授权 */
  IDLE: 'idle',
  /** 信念上持有 tabCapture 授权。注意这只是信念，唯一权威检测是真去调 getMediaStreamId */
  ARMED: 'armed',
  /** streamId 已铸造，等待 offscreen 兑换 ack */
  STARTING: 'starting',
  /** MediaRecorder 正在运行 */
  RECORDING: 'recording',
  /** 已调 stop()，等待最后的 dataavailable + onstop */
  STOPPING: 'stopping',
  /** 拼接分块 + 改写 EBML 头 */
  FINALIZING: 'finalizing',
  /** chrome.downloads.download 进行中 */
  SAVING: 'saving',
  /** 文件已落盘，blob 已释放 */
  DONE: 'done',
  /** 失败。可重试的前提是授权信念仍在 */
  ERROR: 'error',
};

/** 所有合法状态，供校验用 */
export const ALL_STATES = /** @type {TabState[]} */ (Object.values(S));

/**
 * 合法转移表：from → 允许的 to 列表。
 *
 * 几个刻意的设计：
 * - done → starting 合法：**停止捕获不会撤销授权**，所以「再次录制」是一等公民。
 * - error → starting 合法：授权仍在时允许重试。
 * - finalizing → saving 即使 EBML 改写失败也走：降级存未改写的 .webm，绝不丢音频。
 * - 任何状态 → idle：跨源跳转 / 扩展重载会撤销授权，可从任意状态掉回 idle。
 * - 任何状态 → error：兜底，任何环节都可能失败。
 *
 * @type {Record<TabState, TabState[]>}
 */
export const LEGAL = {
  idle: ['armed', 'idle'],
  armed: ['starting', 'idle', 'armed', 'error'],
  starting: ['recording', 'error', 'idle', 'stopping'],
  // 自环是必须的：录制中的进度心跳（bytes、音频活性）以同状态上报，
  // 没有它，每次心跳都会被 setState 拒绝 —— bytes 曾因此永远停在 0
  recording: ['recording', 'stopping', 'error', 'idle'],
  stopping: ['finalizing', 'error', 'idle'],
  finalizing: ['saving', 'error', 'idle'],
  saving: ['done', 'error', 'idle'],
  done: ['starting', 'armed', 'idle', 'done'],
  error: ['starting', 'armed', 'idle', 'error'],
};

/**
 * @param {TabState} from
 * @param {TabState} to
 * @returns {boolean}
 */
export function canTransition(from, to) {
  const allowed = LEGAL[from];
  return Array.isArray(allowed) && allowed.includes(to);
}

/**
 * 断言式转移。非法转移在开发期是 bug，直接抛错以便立刻发现。
 * @param {TabState} from
 * @param {TabState} to
 * @returns {TabState} to
 */
export function assertTransition(from, to) {
  if (!canTransition(from, to)) {
    throw new Error(`非法状态转移: ${from} → ${to}`);
  }
  return to;
}

/** 处于这些状态时，该 Tab 正占用一个 tabCapture 捕获槽位 */
export const ACTIVE_STATES = /** @type {TabState[]} */ ([
  S.STARTING, S.RECORDING, S.STOPPING, S.FINALIZING, S.SAVING,
]);

/** @param {TabState} state */
export function isActive(state) {
  return ACTIVE_STATES.includes(state);
}

/** 处于这些状态时，UI 应显示「忙」而不允许再次点击开始 */
export const BUSY_STATES = ACTIVE_STATES;

/**
 * 错误码。UI 通过 ERROR_MESSAGES 映射为中文文案。
 * @type {Record<string, string>}
 */
export const E = {
  /** getMediaStreamId 拒绝 —— 授权已失效（通常是跨源跳转或扩展重载） */
  GRANT_LOST: 'E_GRANT_LOST',
  /** 该 Tab 已被占用（可能是本扩展重复启动，也可能是别的扩展） */
  TAB_BUSY: 'E_TAB_BUSY',
  /** streamId 未在 10s TTL 内兑换 */
  STREAM_EXPIRED: 'E_STREAM_EXPIRED',
  /** getUserMedia 拒绝 */
  GUM_FAILED: 'E_GUM_FAILED',
  /** Tab 在启动过程中消失 */
  TAB_GONE: 'E_TAB_GONE',
  /** offscreen 文档创建失败 */
  NO_OFFSCREEN: 'E_NO_OFFSCREEN',
  /** MediaRecorder.onerror */
  RECORDER: 'E_RECORDER',
  /** 停止时零分块 */
  NO_DATA: 'E_NO_DATA',
  /** 下载失败或被中断 */
  DOWNLOAD: 'E_DOWNLOAD',
  /** SW 重启后发现的孤儿状态 */
  LOST: 'E_LOST',
  /** EBML 改写失败（已降级存为 .webm，不算丢数据） */
  PATCH_FAILED: 'E_PATCH_FAILED',
  /** IndexedDB 写入失败 */
  STORE: 'E_STORE',
  /** 未归类 */
  UNKNOWN: 'E_UNKNOWN',
};

/**
 * 这些错误在授权信念仍在时可以重试。
 * 注意 GRANT_LOST 不在其中 —— 授权已失效，重试也只会再失败一次，
 * UI 应改为提示用户重新按快捷键授权。
 */
const RETRYABLE = new Set([
  E.TAB_BUSY, E.STREAM_EXPIRED, E.GUM_FAILED, E.RECORDER,
  E.DOWNLOAD, E.LOST, E.NO_OFFSCREEN, E.STORE, E.UNKNOWN,
]);

/** @param {string} code */
export function isRetryable(code) {
  return RETRYABLE.has(code);
}
