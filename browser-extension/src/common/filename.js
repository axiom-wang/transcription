// @ts-check
/**
 * 文件名净化 —— 纯函数，可被 node:test 直接验证。
 *
 * ## 为什么需要单独一个模块
 *
 * Tab 标题是完全不可控的用户数据：可能含 `/`、可能是 100 个汉字、可能是纯 emoji、
 * 可能叫 `CON`、可能以点结尾。而 `chrome.downloads.download` 对非法文件名是直接拒绝，
 * 不是「尽力而为」。一次拒绝就意味着一段录音丢失，所以这里的每条规则都是必要的。
 *
 * ## 最容易被忽略的一条：按字节而非字符截断
 *
 * 文件系统的名称上限通常是 **255 字节**，而每个汉字在 UTF-8 下占 **3 字节**。
 * 所以 `title.slice(0, 100)` 对中文标题来说是错的 —— 100 个汉字是 300 字节，已经超限。
 * 必须按 UTF-8 字节数截断，并且在**字素边界**上切，否则会劈开代理对（emoji）
 * 或 ZWJ 序列（👨‍👩‍👧‍👦），产生乱码。
 */

/** 标题段的 UTF-8 字节上限。留足空间给目录、序号、时间戳和 Chrome 的 " (1)" 去重后缀。 */
export const TITLE_BYTE_LIMIT = 120;

/** Windows 保留设备名（不区分大小写，且带扩展名时同样保留） */
const WINDOWS_RESERVED = new Set([
  'CON', 'PRN', 'AUX', 'NUL',
  'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
  'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9',
]);

const encoder = new TextEncoder();

/** @param {string} s */
export function utf8Length(s) {
  return encoder.encode(s).length;
}

/**
 * 按 UTF-8 字节上限截断，且只在字素边界处切。
 *
 * 用 `Intl.Segmenter` 保证 emoji ZWJ 序列、变体选择符、组合字符都不被劈开。
 * 若运行环境没有 Segmenter（不应发生，Chrome 87+ 和 Node 16+ 都有），
 * 退化为按 code point 迭代 —— 仍然安全（不会劈开代理对），只是可能切开 ZWJ 序列。
 *
 * @param {string} s
 * @param {number} maxBytes
 * @returns {string}
 */
export function truncateUtf8(s, maxBytes) {
  if (utf8Length(s) <= maxBytes) return s;

  /** @type {Iterable<string>} */
  let units;
  if (typeof Intl !== 'undefined' && typeof Intl.Segmenter === 'function') {
    const seg = new Intl.Segmenter(undefined, { granularity: 'grapheme' });
    units = Array.from(seg.segment(s), (part) => part.segment);
  } else {
    units = Array.from(s); // 按 code point，至少不会劈开代理对
  }

  let out = '';
  let used = 0;
  for (const unit of units) {
    const size = utf8Length(unit);
    if (used + size > maxBytes) break;
    out += unit;
    used += size;
  }
  return out;
}

/**
 * 把任意字符串净化为单个安全的路径段（不含分隔符）。
 *
 * 步骤顺序是有讲究的：先归一化再做替换，否则 NFD 分解出的组合字符会被
 * 当成独立字符处理；截断放在最后，因为前面的替换可能改变字节长度。
 *
 * @param {string} raw
 * @param {object} [opts]
 * @param {number} [opts.maxBytes]
 * @param {string} [opts.fallback] 净化后为空时使用的兜底名
 * @returns {string}
 */
export function sanitizeSegment(raw, opts = {}) {
  const maxBytes = opts.maxBytes ?? TITLE_BYTE_LIMIT;
  const fallback = opts.fallback ?? '未命名';

  let s = String(raw ?? '');

  // ① NFC 归一化。macOS/APFS 从文件系统读回的是 NFD（分解形式），
  //    统一成 NFC 能让同一个中文标题在不同系统上产生一致的文件名。
  try {
    s = s.normalize('NFC');
  } catch {
    // normalize 理论上不会抛，但不值得为它让整条保存链路失败
  }

  // ② 剥离控制字符（含 DEL），但**保留** \t \n \v \f \r。
  //    这些是语义上的空白，要留给步骤 ④ 折叠成一个空格 —— 若在这里直接删掉，
  //    "Hello\nWorld" 会变成 "HelloWorld"，把两个词粘在一起。
  s = s.replace(/[\x00-\x08\x0e-\x1f\x7f]/g, '');

  // ③ 替换 Windows/POSIX 的路径与通配符字符。
  //    注意 `/` 必须替换 —— 否则会被 chrome.downloads 当成目录分隔符，
  //    把文件写到意料之外的子目录里。
  s = s.replace(/[<>:"/\\|?*]/g, '_');

  // ④ 折叠所有空白（含全角空格、换行）为单个半角空格并 trim。
  s = s.replace(/\s+/g, ' ').trim();

  // ⑤ 去掉首尾的点和空格 —— Windows 会静默丢弃它们，导致文件名与预期不符。
  s = s.replace(/^[.\s]+/, '').replace(/[.\s]+$/, '');

  // ⑥ Windows 保留设备名加下划线前缀。判断时要去掉扩展名，
  //    因为 `CON.mkv` 在 Windows 上同样是保留名。
  const stem = s.split('.')[0].toUpperCase();
  if (WINDOWS_RESERVED.has(stem)) {
    s = `_${s}`;
  }

  // ⑦ 按 UTF-8 字节截断（CJK 关键步骤，见文件头说明）。
  s = truncateUtf8(s, maxBytes);

  // 截断后可能又在末尾留下空格或点
  s = s.replace(/[.\s]+$/, '');

  // ⑧ 空则兜底
  return s.length > 0 ? s : fallback;
}

/**
 * 时间戳格式化为 `YYYY-MM-DD_HH-mm`，用作会话子目录名。
 * @param {Date} d
 */
export function formatSessionStamp(d) {
  const p = (/** @type {number} */ n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}` +
    `_${p(d.getHours())}-${p(d.getMinutes())}`;
}

/**
 * 时间戳格式化为 `HHmmss`，作为文件名后缀（录制停止时刻）。
 * @param {Date} d
 */
export function formatTimeStamp(d) {
  const p = (/** @type {number} */ n) => String(n).padStart(2, '0');
  return `${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}

/**
 * 组装完整的下载相对路径。
 *
 * 形如：`标签页录音/2026-08-28_14-30/01-某个视频标题-143052.mkv`
 *
 * 按会话建子目录让同批文件聚在一起；序号取授权顺序，于是文件的排序
 * 就等于用户当初构建列表的顺序 —— 这比按字母排序有用得多。
 *
 * 重名不在这里处理：交给 `chrome.downloads` 的 `conflictAction:'uniquify'`，
 * 它会自动追加 ` (1)`，比我们自己枚举文件系统可靠。
 *
 * @param {object} args
 * @param {string} args.title Tab 标题
 * @param {number} args.index 授权顺序（从 1 开始）
 * @param {string} args.sessionStamp 会话目录名
 * @param {Date} args.stoppedAt
 * @param {number} args.tabId 标题净化后为空时的兜底
 * @param {string} [args.rootDir]
 * @param {string} [args.extension]
 * @returns {string}
 */
export function buildDownloadPath(args) {
  const {
    title, index, sessionStamp, stoppedAt, tabId,
    rootDir = '标签页录音', extension = 'mkv',
  } = args;

  const safeTitle = sanitizeSegment(title, { fallback: `标签页-${tabId}` });
  const seq = String(index).padStart(2, '0');
  const time = formatTimeStamp(stoppedAt);

  // rootDir 和 sessionStamp 由我们自己生成，但仍走一遍净化以防日后被参数化
  const safeRoot = sanitizeSegment(rootDir, { fallback: '标签页录音' });
  const safeSession = sanitizeSegment(sessionStamp, { fallback: 'session' });

  return `${safeRoot}/${safeSession}/${seq}-${safeTitle}-${time}.${extension}`;
}

/**
 * ASCII 兜底路径。当 `downloads.download` 因文件名被拒时用它重试一次 ——
 * 绝不让文件名问题弄丢音频。
 *
 * @param {object} args
 * @param {number} args.index
 * @param {string} args.sessionStamp
 * @param {Date} args.stoppedAt
 * @param {string} [args.extension]
 * @returns {string}
 */
export function buildAsciiFallbackPath(args) {
  const { index, sessionStamp, stoppedAt, extension = 'mkv' } = args;
  const seq = String(index).padStart(2, '0');
  // 会话目录名本身只含 ASCII 数字和连字符，但仍剥一遍以确保万无一失
  const safeSession = sessionStamp.replace(/[^\w-]/g, '');
  return `tab-recordings/${safeSession}/${seq}-rec-${formatTimeStamp(stoppedAt)}.${extension}`;
}
