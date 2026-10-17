// @ts-check
/**
 * 分级日志。每个上下文（sw / offscreen / panel）用自己的 tag 创建一个 logger，
 * 这样 DevTools 里能一眼看出消息来自哪个上下文 —— 本扩展有三个独立上下文，
 * 不带 tag 的日志几乎没法调试。
 */

/** @typedef {'debug'|'info'|'warn'|'error'} Level */

/** @type {Record<Level, number>} */
const RANK = { debug: 10, info: 20, warn: 30, error: 40 };

/**
 * 开发期设 'debug'，发布前设 'info'。
 * @type {Level}
 */
export const LEVEL = 'debug';

/**
 * @param {string} tag
 */
export function createLogger(tag) {
  const prefix = `[${tag}]`;

  /** @param {Level} level @param {unknown[]} args */
  const emit = (level, args) => {
    if (RANK[level] < RANK[LEVEL]) return;
    const fn = level === 'debug' ? console.debug
      : level === 'info' ? console.info
      : level === 'warn' ? console.warn
      : console.error;
    fn(prefix, ...args);
  };

  return {
    /** @param {...unknown} args */
    debug: (...args) => emit('debug', args),
    /** @param {...unknown} args */
    info: (...args) => emit('info', args),
    /** @param {...unknown} args */
    warn: (...args) => emit('warn', args),
    /** @param {...unknown} args */
    error: (...args) => emit('error', args),
  };
}

/**
 * 把字节数格式化为人类可读体积。用于 UI 和日志。
 * @param {number} bytes
 */
export function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return '—';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB'];
  let v = bytes / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}

/**
 * 毫秒 → mm:ss 或 h:mm:ss。
 * @param {number} ms
 */
export function formatDuration(ms) {
  if (!Number.isFinite(ms) || ms < 0) ms = 0;
  const total = Math.floor(ms / 1000);
  const s = total % 60;
  const m = Math.floor(total / 60) % 60;
  const h = Math.floor(total / 3600);
  const pad = (/** @type {number} */ n) => String(n).padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

/**
 * 把前 n 个字节格式化为 hex，用于 EBML 解析失败时的诊断日志。
 * @param {Uint8Array} bytes
 * @param {number} [n]
 */
export function hexdump(bytes, n = 64) {
  return Array.from(bytes.subarray(0, n))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join(' ');
}
