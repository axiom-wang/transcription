// @ts-check
/**
 * EBML 头解析与 DocType 改写 —— 把 Chrome 录出的 WebM 变成真正的 Matroska。
 *
 * ## 为什么需要这个模块
 *
 * 陷阱：`MediaRecorder.isTypeSupported('audio/x-matroska;codecs=opus')` 返回 true，
 * 且 `recorder.mimeType` 会照样回显该字符串 —— 但写出的字节里 DocType 仍是 "webm"。
 * 原因是 libwebm 按 **codec id** 决定 DocType（`Segment::DocTypeIsWebm()`），
 * 而 `A_OPUS` 在 WebM 的编解码器白名单里。所以请求 x-matroska 只是个别名，不产生 MKV。
 *
 * 因此：录 `audio/webm;codecs=opus`，然后把 EBML 头里的 DocType 从 "webm" 改成 "matroska"。
 *
 * ## 为什么这样做是安全的（已实测验证，非推测）
 *
 * 1. **SeekHead / Cues 不会失效。** 它们存的 `SeekPosition` / `CueClusterPosition` 是
 *    **段内相对偏移**（RFC 9559 §16：相对于 Segment 元素的 Element Data 起始处），
 *    不是文件绝对偏移。头部长 4 字节会整体推移 Segment，但段内的值一个都不用改。
 *    实测：三个 SeekPosition 条目在 segmentDataStart 从 48 移到 52 后仍全部正确解析。
 * 2. **没有 CRC-32 要重算。** libwebm 从不产出 CRC-32 元素（mkvmuxer.cc 中 `crc` 零命中）。
 * 3. **无长度级联。** payload 31 → 35，两者都是单字节 VINT。
 * 4. **实测无损**：打补丁前后解码出的 PCM 逐位相同。
 *
 * ## 本模块的纪律
 *
 * - 纯函数：`Uint8Array` 进，`Uint8Array` 出。无 DOM、无 chrome.*、无 Blob。
 *   这让它能直接 import 进 node:test，用穷尽向量验证，完全不需要浏览器。
 * - **绝不硬编码偏移 36**：解析 EBML id → 解码 payload size VINT → 遍历子元素定位 DocType。
 * - **绝不复用原 size VINT 宽度**：RFC 8794 允许非最小编码（实测 `0x40 0x1F` 解码为 31、宽度 2），
 *   所有宽度一律重新计算。
 * - **重建而非原地改写**：这样对任何 VINT 宽度变化都免疫。
 */

/** EBML 头元素 ID（保留标记位，按存储形式比较） */
export const ID_EBML = 0x1a45dfa3;
/** DocType */
export const ID_DOCTYPE = 0x4282;
/** DocTypeVersion */
export const ID_DOCTYPE_VERSION = 0x4287;
/** DocTypeReadVersion */
export const ID_DOCTYPE_READ_VERSION = 0x4285;
/** Segment（供测试遍历用） */
export const ID_SEGMENT = 0x18538067;
/** Void —— 合法的填充元素，解析时必须能跳过 */
export const ID_VOID = 0xec;
/** CRC-32 —— libwebm 不产出，但规范允许，解析时必须能跳过 */
export const ID_CRC32 = 0xbf;

const MAX_VINT_OCTETS = 8;

/** 头部字节不够，需要调用方切一片更大的再来 */
export class EbmlNeedMoreBytes extends Error {
  /** @param {number} needAtLeast 至少需要这么多字节 */
  constructor(needAtLeast) {
    super(`EBML 头部字节不足，至少需要 ${needAtLeast} 字节`);
    this.name = 'EbmlNeedMoreBytes';
    /** @type {number} */
    this.needAtLeast = needAtLeast;
  }
}

/** 字节不是合法的 EBML 头 */
export class EbmlParseError extends Error {
  /** @param {string} message */
  constructor(message) {
    super(message);
    this.name = 'EbmlParseError';
  }
}

/**
 * 从 VINT 首字节得出其总字节宽度。
 * RFC 8794：首字节的前导零个数决定宽度，第一个 1 位是 VINT_MARKER。
 * @param {number} firstByte
 * @returns {number} 1..8；0 表示非法（首字节为 0，宽度会超过 8 字节）
 */
export function readVintWidth(firstByte) {
  if (!Number.isInteger(firstByte) || firstByte < 0 || firstByte > 0xff) {
    throw new EbmlParseError(`VINT 首字节非法: ${firstByte}`);
  }
  if (firstByte === 0) return 0; // 宽度 > 8，本实现不支持（Matroska 实际用不到）
  // 最高位为 1 → 宽度 1；否则每多一个前导零，宽度 +1
  let mask = 0x80;
  for (let width = 1; width <= MAX_VINT_OCTETS; width += 1) {
    if (firstByte & mask) return width;
    mask >>= 1;
  }
  return 0;
}

/**
 * 读一个 VINT 的**值**（去掉标记位）。用于 Element Data Size。
 *
 * `unknown` 为 true 表示所有值位都是 1，按 RFC 8794 这代表「未知长度」。
 * Chrome 的 live 模式就会给 Segment 写未知长度（`01 FF FF FF FF FF FF FF`）。
 *
 * @param {Uint8Array} bytes
 * @param {number} pos
 * @returns {{value:number, width:number, unknown:boolean}}
 */
export function readVint(bytes, pos) {
  if (pos >= bytes.length) throw new EbmlNeedMoreBytes(pos + 1);
  const first = bytes[pos];
  const width = readVintWidth(first);
  if (width === 0) {
    throw new EbmlParseError(`不支持的 VINT 宽度（首字节 0x${first.toString(16)}，超过 8 字节）`);
  }
  if (pos + width > bytes.length) throw new EbmlNeedMoreBytes(pos + width);

  // 去掉 VINT_MARKER 位后的首字节值位
  let value = first & ((1 << (8 - width)) - 1);
  // 首字节的值位是否全为 1（判断 unknown 用）
  let allOnes = value === ((1 << (8 - width)) - 1);

  for (let i = 1; i < width; i += 1) {
    const b = bytes[pos + i];
    // 用乘法而非 <<8，避免 32 位有符号移位在宽 VINT 上溢出
    value = value * 256 + b;
    if (b !== 0xff) allOnes = false;
  }

  // 必须**先**判 unknown 再做范围检查：未知长度的标准写法是
  // `01 FF FF FF FF FF FF FF`，其数值为 2^56-1，会超出 Number.isSafeInteger，
  // 但这个数值本身没有意义。Chrome 的 live 模式给 Segment 写的正是这个字节序列，
  // 也就是本扩展的主录制路径 —— 顺序颠倒会让所有录音在解析时直接抛错。
  // 返回 value: 0 是防御性的：调用方若忘记检查 unknown，也不会把它误当成真实长度。
  if (allOnes) {
    return { value: 0, width, unknown: true };
  }

  if (!Number.isSafeInteger(value)) {
    throw new EbmlParseError('VINT 值超出安全整数范围');
  }
  return { value, width, unknown: false };
}

/**
 * 把长度编码为**最小宽度**的 size VINT。
 *
 * 微妙之处：若某宽度下值恰好是全 1，该编码被 RFC 8794 保留表示「未知长度」，
 * 因此必须提升到下一个宽度。典型例子：127 不能编码为 `0x7F`（那是 unknown），
 * 必须写成 `0x40 0x7F`。这个用例在测试里有专门向量。
 *
 * @param {number} value
 * @returns {Uint8Array}
 */
export function encodeVintSize(value) {
  if (!Number.isInteger(value) || value < 0) {
    throw new EbmlParseError(`size VINT 值非法: ${value}`);
  }
  for (let width = 1; width <= MAX_VINT_OCTETS; width += 1) {
    const bits = 7 * width;
    // 该宽度可表示的最大值（全 1，保留给 unknown）
    const allOnes = Math.pow(2, bits) - 1;
    if (value < allOnes) {
      const out = new Uint8Array(width);
      let v = value;
      for (let i = width - 1; i >= 1; i -= 1) {
        out[i] = v % 256;
        v = Math.floor(v / 256);
      }
      // 首字节 = VINT_MARKER | 剩余高位值
      out[0] = v | (0x80 >> (width - 1));
      return out;
    }
  }
  throw new EbmlParseError(`size VINT 值过大，无法用 8 字节编码: ${value}`);
}

/**
 * 读元素 ID。**ID 保留标记位**（按存储形式比较），这与 size VINT 的处理不同 ——
 * 规范中 Element ID 就是以含标记位的形式书写和比较的（如 EBML 头是 0x1A45DFA3）。
 *
 * @param {Uint8Array} bytes
 * @param {number} pos
 * @returns {{id:number, width:number}}
 */
export function readElementId(bytes, pos) {
  if (pos >= bytes.length) throw new EbmlNeedMoreBytes(pos + 1);
  const first = bytes[pos];
  const width = readVintWidth(first);
  if (width === 0 || width > 4) {
    // EBMLMaxIDLength 为 4；更宽的 ID 在 Matroska 里不合法
    throw new EbmlParseError(`元素 ID 宽度非法: ${width}（首字节 0x${first.toString(16)}）`);
  }
  if (pos + width > bytes.length) throw new EbmlNeedMoreBytes(pos + width);

  let id = 0;
  for (let i = 0; i < width; i += 1) {
    id = id * 256 + bytes[pos + i];
  }
  return { id, width };
}

/**
 * @typedef {object} EbmlChild
 * @property {number} id 元素 ID（含标记位）
 * @property {number} idStart ID 首字节在 bytes 中的偏移
 * @property {number} idWidth ID 字节数
 * @property {number} sizeStart size VINT 首字节偏移
 * @property {number} sizeWidth size VINT 字节数
 * @property {number} payloadStart 负载首字节偏移
 * @property {number} size 负载字节数
 */

/**
 * 解析 EBML 头，返回其结构与所有子元素。
 *
 * 遍历时**容忍并跳过未知子元素**（Void、CRC-32 等），因此本函数天然与子元素顺序无关 ——
 * 不依赖 DocType 出现在第几个。
 *
 * @param {Uint8Array} bytes 文件开头的若干字节（至少要覆盖完整 EBML 头）
 * @returns {{headerLen:number, bodyStart:number, bodyEnd:number, children:EbmlChild[]}}
 */
export function parseEbmlHeader(bytes) {
  const { id, width: idWidth } = readElementId(bytes, 0);
  if (id !== ID_EBML) {
    throw new EbmlParseError(
      `文件不以 EBML 头开始（读到 ID 0x${id.toString(16)}，期望 0x1a45dfa3）`,
    );
  }

  const size = readVint(bytes, idWidth);
  if (size.unknown) {
    throw new EbmlParseError('EBML 头长度为未知长度，不合法');
  }

  const bodyStart = idWidth + size.width;
  const bodyEnd = bodyStart + size.value;
  if (bodyEnd > bytes.length) {
    // 调用方应切一片更大的重来
    throw new EbmlNeedMoreBytes(bodyEnd);
  }

  /** @type {EbmlChild[]} */
  const children = [];
  let pos = bodyStart;
  while (pos < bodyEnd) {
    const cid = readElementId(bytes, pos);
    const csize = readVint(bytes, pos + cid.width);
    if (csize.unknown) {
      throw new EbmlParseError(
        `EBML 头内的子元素 0x${cid.id.toString(16)} 为未知长度，不合法`,
      );
    }
    const payloadStart = pos + cid.width + csize.width;
    const next = payloadStart + csize.value;
    if (next > bodyEnd) {
      throw new EbmlParseError(
        `子元素 0x${cid.id.toString(16)} 越出 EBML 头边界（${next} > ${bodyEnd}）`,
      );
    }
    children.push({
      id: cid.id,
      idStart: pos,
      idWidth: cid.width,
      sizeStart: pos + cid.width,
      sizeWidth: csize.width,
      payloadStart,
      size: csize.value,
    });
    pos = next;
  }

  return { headerLen: bodyEnd, bodyStart, bodyEnd, children };
}

/**
 * 构造一个把 DocType 替换为 `docType` 的新 EBML 头。
 *
 * **重建而非原地改写**：把 DocType 之前的字节、新的 DocType 元素、之后的字节拼起来，
 * 再重算外层 size VINT。这样对任何 VINT 宽度变化都免疫，不需要假设「+4 字节」。
 *
 * @param {Uint8Array} headBytes 文件开头的若干字节
 * @param {string} [docType]
 * @returns {{header:Uint8Array, oldHeaderLen:number, oldDocType:string, changed:boolean}}
 */
export function buildPatchedHeader(headBytes, docType = 'matroska') {
  const parsed = parseEbmlHeader(headBytes);
  const dt = parsed.children.find((c) => c.id === ID_DOCTYPE);
  if (!dt) {
    throw new EbmlParseError('EBML 头中找不到 DocType 元素');
  }

  const oldDocType = new TextDecoder().decode(
    headBytes.subarray(dt.payloadStart, dt.payloadStart + dt.size),
  );

  if (oldDocType === docType) {
    // 已经是目标 DocType，无需改写（幂等）
    return {
      header: headBytes.slice(0, parsed.headerLen),
      oldHeaderLen: parsed.headerLen,
      oldDocType,
      changed: false,
    };
  }

  const idBytes = headBytes.subarray(dt.idStart, dt.idStart + dt.idWidth);
  const payload = new TextEncoder().encode(docType);
  const sizeBytes = encodeVintSize(payload.length);

  // 新的头部负载 = [DocType 之前] + [新 DocType 元素] + [DocType 之后]
  const before = headBytes.subarray(parsed.bodyStart, dt.idStart);
  const after = headBytes.subarray(dt.payloadStart + dt.size, parsed.bodyEnd);

  const bodyLen = before.length + idBytes.length + sizeBytes.length + payload.length + after.length;
  const body = new Uint8Array(bodyLen);
  let o = 0;
  body.set(before, o); o += before.length;
  body.set(idBytes, o); o += idBytes.length;
  body.set(sizeBytes, o); o += sizeBytes.length;
  body.set(payload, o); o += payload.length;
  body.set(after, o);

  // 外层：EBML ID（原样）+ 重新编码的 size + 新负载
  const ebmlId = headBytes.subarray(0, readElementId(headBytes, 0).width);
  const outerSize = encodeVintSize(body.length);
  const header = new Uint8Array(ebmlId.length + outerSize.length + body.length);
  header.set(ebmlId, 0);
  header.set(outerSize, ebmlId.length);
  header.set(body, ebmlId.length + outerSize.length);

  return {
    header,
    oldHeaderLen: parsed.headerLen,
    oldDocType,
    changed: true,
  };
}

/**
 * 便利函数：对**完整**字节数组做改写。仅用于测试和 tools/ 下的命令行工具 ——
 * 生产路径请用 offscreen/mkv.js 的 Blob 版本以保持 O(1) 内存。
 *
 * @param {Uint8Array} bytes
 * @param {string} [docType]
 * @returns {Uint8Array}
 */
export function patchBytes(bytes, docType = 'matroska') {
  const r = buildPatchedHeader(bytes, docType);
  if (!r.changed) return bytes;
  const rest = bytes.subarray(r.oldHeaderLen);
  const out = new Uint8Array(r.header.length + rest.length);
  out.set(r.header, 0);
  out.set(rest, r.header.length);
  return out;
}
