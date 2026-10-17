// @ts-check
/**
 * 基于 common/ebml.js 的 Blob 薄封装 —— WebM → Matroska。
 *
 * ## 唯一的纪律：O(1) 内存
 *
 * 只把**开头几百字节**读进内存做解析，剩下的全部靠 `blob.slice()` 引用。
 * `blob.slice()` **不会实体化字节**，而 `Blob` 构造器是按引用持有分片的 ——
 * 所以一段 2 小时的录音也只需要约 256 字节的实际分配。
 *
 * ⚠️ **绝不要对完整录音调 `.arrayBuffer()`。** 一次就能让 offscreen 文档
 * 因为几百 MB 的堆分配而崩溃，而且崩溃时正在录的所有 Tab 会一起完蛋。
 */

import {
  buildPatchedHeader,
  EbmlNeedMoreBytes,
  EbmlParseError,
} from '../common/ebml.js';
import { createLogger, hexdump } from '../common/log.js';

const log = createLogger('mkv');

/** 与 .mkv 扩展名匹配的 MIME，避免 chrome.downloads 改写扩展名 */
export const MKV_MIME = 'video/x-matroska';
/** 降级路径用的原始 MIME */
export const WEBM_MIME = 'audio/webm';

/** 首次探测的字节数。Chrome 的头部是 36 字节，256 留足余量。 */
const PROBE_BYTES = 256;
/** 探测不足时的重试字节数（够容纳任何合理的 EBML 头，含大量 Void 填充） */
const RETRY_BYTES = 4096;

/**
 * @typedef {object} PatchResult
 * @property {Blob} blob 可直接下载的 Blob
 * @property {string} mime
 * @property {string} extension 'mkv' 或降级时的 'webm'
 * @property {boolean} patched 是否真的改写了 DocType
 * @property {string} [warning] 降级原因（中文，可直接显示给用户）
 */

/**
 * 把 MediaRecorder 产出的 WebM Blob 改写为 Matroska。
 *
 * **失败绝不丢数据**：任何解析异常都降级为「原样保存为 .webm」并附带中文警告。
 * 为一个头部边缘情况丢掉两小时录音是不可接受的，而未改写的 WebM
 * 在所有基于 libavformat 的播放器（ffmpeg / VLC / mpv）里都能正常播放。
 *
 * @param {Blob} blob
 * @param {object} [opts]
 * @param {string} [opts.docType]
 * @returns {Promise<PatchResult>}
 */
export async function webmBlobToMatroska(blob, opts = {}) {
  const docType = opts.docType ?? 'matroska';

  try {
    let head = new Uint8Array(await blob.slice(0, PROBE_BYTES).arrayBuffer());
    let result;

    try {
      result = buildPatchedHeader(head, docType);
    } catch (err) {
      if (!(err instanceof EbmlNeedMoreBytes)) throw err;
      // 头部比预期长（比如塞了大段 Void）—— 切一片更大的重试一次
      log.debug(`头部需要 ${err.needAtLeast} 字节，重试 ${RETRY_BYTES}`);
      head = new Uint8Array(await blob.slice(0, RETRY_BYTES).arrayBuffer());
      result = buildPatchedHeader(head, docType);
    }

    if (!result.changed) {
      // 已经是目标 DocType（理论上不会发生，除非 Chrome 改了行为）。
      // 只换 MIME，字节不动。
      log.info(`DocType 已是 ${docType}，无需改写`);
      return {
        blob: blob.slice(0, blob.size, MKV_MIME),
        mime: MKV_MIME, extension: 'mkv', patched: false,
      };
    }

    // ★ 关键的 O(1) 拼接：新头部（几十字节）+ 原 Blob 的引用切片
    const out = new Blob([result.header, blob.slice(result.oldHeaderLen)], { type: MKV_MIME });

    log.info(
      `DocType "${result.oldDocType}" → "${docType}"；` +
      `头部 ${result.oldHeaderLen} → ${result.header.length} 字节`,
    );

    return { blob: out, mime: MKV_MIME, extension: 'mkv', patched: true };
  } catch (err) {
    // ── 降级路径：保住音频，牺牲容器格式 ──
    const reason = err instanceof EbmlParseError || err instanceof EbmlNeedMoreBytes
      ? err.message
      : String(err);

    // 把头部字节打出来，这是日后诊断这类问题的唯一线索
    try {
      const head = new Uint8Array(await blob.slice(0, 64).arrayBuffer());
      log.error(`EBML 改写失败：${reason}\n头部字节: ${hexdump(head, 64)}`);
    } catch {
      log.error(`EBML 改写失败：${reason}（且无法读取头部字节）`);
    }

    return {
      blob: blob.slice(0, blob.size, WEBM_MIME),
      mime: WEBM_MIME,
      extension: 'webm',
      patched: false,
      warning: '已保存为 .webm（容器改写失败，音频完好）',
    };
  }
}
