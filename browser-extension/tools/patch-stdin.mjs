#!/usr/bin/env node
// @ts-check
/**
 * 把 stdin 上的 WebM 流改写为 Matroska，写到 stdout。
 *
 * 用于 M1 的端到端验证管道（见 README「验证」一节）：
 *
 *   ffmpeg -f lavfi -i "sine=frequency=440:duration=3" -c:a libopus -f webm pipe:1 \
 *     | node tools/patch-stdin.mjs \
 *     | ffmpeg -i pipe:0 -f s16le -c:a pcm_s16le pipe:1 | md5sum
 *
 * 该 md5 必须与不经过本脚本的同一管道完全一致 —— 这证明改写是无损的。
 *
 * 注意：本工具会把整个流读入内存，因为它只用于测试小样本。
 * 生产路径请用 src/offscreen/mkv.js 的 Blob 版本，它保持 O(1) 内存。
 */

import { buildPatchedHeader, EbmlNeedMoreBytes } from '../src/common/ebml.js';

/** @param {NodeJS.ReadableStream} stream */
async function readAll(stream) {
  /** @type {Buffer[]} */
  const parts = [];
  for await (const chunk of stream) {
    parts.push(typeof chunk === 'string' ? Buffer.from(chunk) : chunk);
  }
  return new Uint8Array(Buffer.concat(parts));
}

const docType = process.argv[2] ?? 'matroska';
const input = await readAll(process.stdin);

if (input.length === 0) {
  process.stderr.write('错误：stdin 为空\n');
  process.exit(1);
}

let result;
try {
  result = buildPatchedHeader(input, docType);
} catch (err) {
  if (err instanceof EbmlNeedMoreBytes) {
    process.stderr.write(`错误：输入过短，EBML 头需要至少 ${err.needAtLeast} 字节\n`);
  } else {
    process.stderr.write(`错误：${err instanceof Error ? err.message : String(err)}\n`);
  }
  process.exit(1);
}

if (!result.changed) {
  process.stderr.write(`DocType 已是 "${docType}"，原样输出\n`);
  process.stdout.write(input);
} else {
  process.stderr.write(
    `DocType "${result.oldDocType}" → "${docType}"；` +
    `头部 ${result.oldHeaderLen} → ${result.header.length} 字节\n`,
  );
  process.stdout.write(result.header);
  process.stdout.write(input.subarray(result.oldHeaderLen));
}
