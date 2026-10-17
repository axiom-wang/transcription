// @ts-check
/**
 * EBML 补丁模块的单测。
 *
 * 这些测试**完全不需要浏览器** —— 这是把 ebml.js 写成纯函数的全部意义所在。
 * 用 `node --test tests/` 运行。
 *
 * 最重要的是最后一组「结构不变量」测试：它对真实的 ffmpeg 产出的 WebM 打补丁，
 * 然后遍历 SeekHead，断言每个 SeekPosition 相对**新的** segmentDataStart 仍能
 * 解析到它记录的那个元素 ID。这正是「改写头部不会破坏 seek 结构」这一安全论证的
 * 可执行版本 —— 手工验证过一次，固化成测试就能永久守住。
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
  ID_EBML,
  ID_DOCTYPE,
  ID_SEGMENT,
  ID_VOID,
  EbmlNeedMoreBytes,
  EbmlParseError,
  readVintWidth,
  readVint,
  encodeVintSize,
  readElementId,
  parseEbmlHeader,
  buildPatchedHeader,
  patchBytes,
} from '../src/common/ebml.js';

const HERE = dirname(fileURLToPath(import.meta.url));

/** @param {string} hex */
const bytes = (hex) =>
  new Uint8Array(hex.trim().split(/\s+/).map((h) => parseInt(h, 16)));

/** @param {Uint8Array} b */
const hex = (b) => Array.from(b).map((x) => x.toString(16).padStart(2, '0')).join(' ');

/**
 * Chrome/libwebm 实际写出的 36 字节头部（金标准）。
 * 已用真实录制产物核对过字节。
 */
const CHROME_HEADER = bytes(`
  1a 45 df a3 9f
  42 86 81 01
  42 f7 81 01
  42 f2 81 04
  42 f3 81 08
  42 82 84 77 65 62 6d
  42 87 81 04
  42 85 81 02
`);

/** 期望的改写结果：40 字节，payload 35（0xa3），DocType 变为 8 字节的 "matroska" */
const EXPECTED_PATCHED = bytes(`
  1a 45 df a3 a3
  42 86 81 01
  42 f7 81 01
  42 f2 81 04
  42 f3 81 08
  42 82 88 6d 61 74 72 6f 73 6b 61
  42 87 81 04
  42 85 81 02
`);

// ───────────────────────────── VINT 基础 ─────────────────────────────

test('readVintWidth: 前导零个数决定宽度', () => {
  assert.equal(readVintWidth(0x80), 1);
  assert.equal(readVintWidth(0xff), 1);
  assert.equal(readVintWidth(0x40), 2);
  assert.equal(readVintWidth(0x7f), 2);
  assert.equal(readVintWidth(0x20), 3);
  assert.equal(readVintWidth(0x10), 4);
  assert.equal(readVintWidth(0x08), 5);
  assert.equal(readVintWidth(0x01), 8);
  // 首字节 0 表示宽度 > 8，本实现不支持
  assert.equal(readVintWidth(0x00), 0);
});

test('encodeVintSize / readVint 往返一致', () => {
  // 127 是微妙情形：0x7F 的值位全为 1，被保留表示 unknown，
  // 因此必须提升到 2 字节写成 40 7F。
  const cases = [0, 1, 4, 31, 35, 126, 127, 128, 16383, 16384, 2 ** 21 - 2];
  for (const v of cases) {
    const enc = encodeVintSize(v);
    const dec = readVint(enc, 0);
    assert.equal(dec.value, v, `值 ${v} 往返失败（编码 ${hex(enc)}）`);
    assert.equal(dec.width, enc.length, `值 ${v} 宽度不一致`);
    assert.equal(dec.unknown, false, `值 ${v} 不应被判为 unknown（编码 ${hex(enc)}）`);
  }
});

test('encodeVintSize: 全 1 值必须提升宽度（避免与 unknown 混淆）', () => {
  // 126 = 0x7E，单字节 0xFE 可表示且不是全 1 → 宽度 1
  assert.equal(hex(encodeVintSize(126)), 'fe');
  // 127 单字节会是 0x7F（全 1 = unknown）→ 必须提升到 2 字节
  assert.equal(hex(encodeVintSize(127)), '40 7f');
  // 2^14-1 = 16383 在 2 字节下是全 1 → 提升到 3 字节
  // 3 字节编码：0x20 标记位 + 值 16383 的低 14 位 → 20 3f ff
  assert.equal(hex(encodeVintSize(16383)), '20 3f ff');
  // 16384 在 3 字节下不是全 1，仍是 3 字节
  assert.equal(hex(encodeVintSize(16384)), '20 40 00');
});

test('readVint: 识别未知长度（值位全为 1）', () => {
  // Chrome live 模式给 Segment 写的就是这个：01 FF FF FF FF FF FF FF
  const unknown = bytes('01 ff ff ff ff ff ff ff');
  const r = readVint(unknown, 0);
  assert.equal(r.width, 8);
  assert.equal(r.unknown, true);

  // 单字节 unknown
  const u1 = readVint(bytes('ff'), 0);
  assert.equal(u1.unknown, true);
  assert.equal(u1.width, 1);
});

test('readVint: 接受非最小（冗余）编码', () => {
  // RFC 8794 明确允许冗余长度编码。实测 libwebm 可能写出这种形式。
  // 0x40 0x1F 是 31 的 2 字节写法（最小写法是 0x9F）
  const r = readVint(bytes('40 1f'), 0);
  assert.equal(r.value, 31);
  assert.equal(r.width, 2);
  assert.equal(r.unknown, false);
});

test('readElementId: 保留标记位', () => {
  const r = readElementId(bytes('1a 45 df a3'), 0);
  assert.equal(r.id, ID_EBML);
  assert.equal(r.width, 4);

  const dt = readElementId(bytes('42 82'), 0);
  assert.equal(dt.id, ID_DOCTYPE);
  assert.equal(dt.width, 2);

  // 单字节 ID（Void）
  const v = readElementId(bytes('ec'), 0);
  assert.equal(v.id, ID_VOID);
  assert.equal(v.width, 1);
});

test('readElementId: 拒绝超过 4 字节的 ID（EBMLMaxIDLength = 4）', () => {
  assert.throws(() => readElementId(bytes('08 00 00 00 00'), 0), EbmlParseError);
});

// ───────────────────────── 金标准改写向量 ─────────────────────────

test('金标准：Chrome 的 36 字节头 → 40 字节 matroska 头', () => {
  const r = buildPatchedHeader(CHROME_HEADER, 'matroska');

  assert.equal(r.changed, true);
  assert.equal(r.oldDocType, 'webm');
  assert.equal(r.oldHeaderLen, 36, '原头部应为 36 字节');
  assert.equal(r.header.length, 40, '新头部应为 40 字节');
  assert.equal(hex(r.header), hex(EXPECTED_PATCHED), '改写后字节与期望不一致');

  // 单字节 VINT，无长度级联：size 字节从 0x9f（31）变为 0xa3（35）
  assert.equal(r.header[4], 0xa3);
});

test('金标准：改写后的头能被重新解析，且 DocType 正确', () => {
  const r = buildPatchedHeader(CHROME_HEADER);
  const reparsed = parseEbmlHeader(r.header);
  const dt = reparsed.children.find((c) => c.id === ID_DOCTYPE);
  assert.ok(dt, '改写后应仍能找到 DocType');
  const value = new TextDecoder().decode(
    r.header.subarray(dt.payloadStart, dt.payloadStart + dt.size),
  );
  assert.equal(value, 'matroska');
  assert.equal(reparsed.headerLen, 40);

  // 其余字段必须原样保留 —— DocTypeVersion=4 / DocTypeReadVersion=2 本身已是合法
  // Matroska 值，不该被动过（FFmpeg 校验的是 ReadVersion，会忽略 Version）
  const ids = reparsed.children.map((c) => c.id);
  assert.deepEqual(ids, [0x4286, 0x42f7, 0x42f2, 0x42f3, 0x4282, 0x4287, 0x4285]);
});

test('幂等：已是 matroska 时 changed 为 false 且字节不动', () => {
  const once = patchBytes(CHROME_HEADER);
  const r = buildPatchedHeader(once);
  assert.equal(r.changed, false);
  assert.equal(r.oldDocType, 'matroska');
  assert.equal(hex(patchBytes(once)), hex(once), '二次改写不应改变字节');
});

test('非最小 size VINT：能解析，并重新发出为最小编码', () => {
  // 手工构造：外层 size 用 2 字节冗余编码 40 1F（值 31）
  const nonMinimal = bytes(`
    1a 45 df a3 40 1f
    42 86 81 01
    42 f7 81 01
    42 f2 81 04
    42 f3 81 08
    42 82 84 77 65 62 6d
    42 87 81 04
    42 85 81 02
  `);
  // 原头部 = 4(ID) + 2(size) + 31(payload) = 37 字节
  const parsed = parseEbmlHeader(nonMinimal);
  assert.equal(parsed.headerLen, 37);

  const r = buildPatchedHeader(nonMinimal);
  assert.equal(r.oldHeaderLen, 37);
  assert.equal(r.oldDocType, 'webm');
  // 重新编码时用最小宽度 → 4 + 1 + 35 = 40
  assert.equal(r.header.length, 40, '应重新编码为最小宽度 size VINT');
  assert.equal(r.header[4], 0xa3);
});

test('DocType 之前有 Void 元素时仍能定位', () => {
  // Void（0xEC）长度 3，插在 DocType 之前。payload 从 31 变为 31+5=36 → 0xa4
  const withVoid = bytes(`
    1a 45 df a3 a4
    42 86 81 01
    42 f7 81 01
    42 f2 81 04
    42 f3 81 08
    ec 83 00 00 00
    42 82 84 77 65 62 6d
    42 87 81 04
    42 85 81 02
  `);
  const parsed = parseEbmlHeader(withVoid);
  assert.ok(parsed.children.some((c) => c.id === ID_VOID), '应解析出 Void 元素');

  const r = buildPatchedHeader(withVoid);
  assert.equal(r.changed, true);
  assert.equal(r.oldDocType, 'webm');

  // Void 必须被保留下来（改写只动 DocType）
  const reparsed = parseEbmlHeader(r.header);
  assert.ok(reparsed.children.some((c) => c.id === ID_VOID), 'Void 元素应被保留');
  const dt = reparsed.children.find((c) => c.id === ID_DOCTYPE);
  assert.ok(dt);
  assert.equal(
    new TextDecoder().decode(r.header.subarray(dt.payloadStart, dt.payloadStart + dt.size)),
    'matroska',
  );
});

// ───────────────────────────── 错误路径 ─────────────────────────────

test('截断的头 → EbmlNeedMoreBytes，且 needAtLeast 正确', () => {
  const truncated = CHROME_HEADER.subarray(0, 20);
  assert.throws(
    () => parseEbmlHeader(truncated),
    (/** @type {any} */ err) => {
      assert.ok(err instanceof EbmlNeedMoreBytes, `应抛 EbmlNeedMoreBytes，实际 ${err.name}`);
      assert.equal(err.needAtLeast, 36, 'needAtLeast 应为完整头长度 36');
      return true;
    },
  );
});

test('只有 1 字节 → EbmlNeedMoreBytes', () => {
  assert.throws(() => parseEbmlHeader(bytes('1a')), EbmlNeedMoreBytes);
});

test('非 EBML 字节 → EbmlParseError', () => {
  // ID4CT... 之类的随机数据
  assert.throws(() => parseEbmlHeader(bytes('80 81 82 83 84 85')), EbmlParseError);
  // 正确宽度但错误的 ID
  assert.throws(() => parseEbmlHeader(bytes('1a 45 df a4 9f 00')), EbmlParseError);
});

test('EBML 头为未知长度 → EbmlParseError', () => {
  assert.throws(() => parseEbmlHeader(bytes('1a 45 df a3 ff 00 00')), EbmlParseError);
});

test('缺少 DocType → EbmlParseError', () => {
  // 只有 EBMLVersion，没有 DocType
  const noDocType = bytes('1a 45 df a3 84 42 86 81 01');
  assert.throws(() => buildPatchedHeader(noDocType), EbmlParseError);
});

test('子元素越出头部边界 → EbmlParseError', () => {
  // 头部声明 payload 4 字节（84）→ bodyEnd = 9。
  // 子元素 42 86 的 size VINT 是 88（值 8）→ 子元素总长 = 2+1+8 = 11，末端 16 > 9，越界。
  assert.throws(() => parseEbmlHeader(bytes('1a 45 df a3 84 42 86 88 00 00')), EbmlParseError);
});

test('声明长度恰好用尽头部边界时不应报错（边界相等是合法的）', () => {
  // payload 4 字节，子元素 42 86 81 01 正好 4 字节 —— 严格相等不是越界。
  // 这条守住 parseEbmlHeader 里 `next > bodyEnd` 不能写成 `next >= bodyEnd`。
  const exact = bytes('1a 45 df a3 84 42 86 81 01');
  const parsed = parseEbmlHeader(exact);
  assert.equal(parsed.headerLen, 9);
  assert.equal(parsed.children.length, 1);
  assert.equal(parsed.children[0].id, 0x4286);
});

// ─────────────── 结构不变量：SeekHead 在改写后仍然有效 ───────────────

/**
 * 读一个 uinteger 元素的负载（大端无符号整数）。
 * 注意：这与 size VINT 不同 —— uinteger 负载没有标记位。
 * @param {Uint8Array} b @param {number} start @param {number} size
 */
function readUInt(b, start, size) {
  let v = 0;
  for (let i = 0; i < size; i += 1) v = v * 256 + b[start + i];
  return v;
}

/**
 * 在 [start, end) 区间内遍历同级元素。
 * @param {Uint8Array} b
 * @param {number} start
 * @param {number} end
 * @returns {Array<{id:number, payloadStart:number, size:number, elementStart:number}>}
 */
function walkLevel(b, start, end) {
  /** @type {Array<{id:number, payloadStart:number, size:number, elementStart:number}>} */
  const out = [];
  let pos = start;
  while (pos < end) {
    const elementStart = pos;
    const id = readElementId(b, pos);
    const size = readVint(b, pos + id.width);
    const payloadStart = pos + id.width + size.width;
    if (size.unknown) {
      // 未知长度（live 模式的 Segment）：把剩余区间都算作它的负载
      out.push({ id: id.id, payloadStart, size: end - payloadStart, elementStart });
      break;
    }
    out.push({ id: id.id, payloadStart, size: size.value, elementStart });
    pos = payloadStart + size.value;
  }
  return out;
}

const ID_SEEK_HEAD = 0x114d9b74;
const ID_SEEK = 0x4dbb;
const ID_SEEK_ID = 0x53ab;
const ID_SEEK_POSITION = 0x53ac;

/**
 * 找到 Segment 的 Element Data 起始偏移（即所有 SeekPosition 的基准点）。
 * @param {Uint8Array} b
 */
function findSegmentDataStart(b) {
  const header = parseEbmlHeader(b);
  const top = walkLevel(b, header.headerLen, b.length);
  const seg = top.find((e) => e.id === ID_SEGMENT);
  assert.ok(seg, '应能找到 Segment 元素');
  return seg.payloadStart;
}

/**
 * 收集 SeekHead 里的所有 (SeekID, SeekPosition) 对。
 * @param {Uint8Array} b
 * @param {number} segmentDataStart
 */
function collectSeekEntries(b, segmentDataStart) {
  const level1 = walkLevel(b, segmentDataStart, b.length);
  const seekHeads = level1.filter((e) => e.id === ID_SEEK_HEAD);
  /** @type {Array<{seekId:number, position:number}>} */
  const entries = [];
  for (const sh of seekHeads) {
    for (const seek of walkLevel(b, sh.payloadStart, sh.payloadStart + sh.size)) {
      if (seek.id !== ID_SEEK) continue;
      const fields = walkLevel(b, seek.payloadStart, seek.payloadStart + seek.size);
      const idField = fields.find((f) => f.id === ID_SEEK_ID);
      const posField = fields.find((f) => f.id === ID_SEEK_POSITION);
      if (!idField || !posField) continue;
      entries.push({
        seekId: readUInt(b, idField.payloadStart, idField.size),
        position: readUInt(b, posField.payloadStart, posField.size),
      });
    }
  }
  return entries;
}

test('结构不变量：改写后每个 SeekPosition 仍解析到其记录的元素 ID', () => {
  const raw = new Uint8Array(readFileSync(join(HERE, 'fixtures/tone-2s.webm')));

  // ① 改写前：建立基准
  const beforeSegStart = findSegmentDataStart(raw);
  const beforeEntries = collectSeekEntries(raw, beforeSegStart);
  assert.ok(beforeEntries.length > 0, 'fixture 应含 SeekHead 条目（否则本测试无意义）');

  for (const e of beforeEntries) {
    const target = readElementId(raw, beforeSegStart + e.position);
    assert.equal(
      target.id, e.seekId,
      `改写前 SeekPosition ${e.position} 应指向 0x${e.seekId.toString(16)}，` +
      `实际 0x${target.id.toString(16)}`,
    );
  }

  // ② 打补丁
  const patched = patchBytes(raw, 'matroska');
  assert.equal(patched.length, raw.length + 4, '头部应恰好增长 4 字节');

  // ③ 改写后：Segment 数据起点前移了，但 SeekPosition 的**值**一个都没变，
  //    因为它们是段内相对偏移（RFC 9559 §16）—— 这正是整个安全论证的核心。
  const afterSegStart = findSegmentDataStart(patched);
  assert.equal(afterSegStart, beforeSegStart + 4, 'Segment 数据起点应后移 4 字节');

  const afterEntries = collectSeekEntries(patched, afterSegStart);
  assert.deepEqual(
    afterEntries, beforeEntries,
    'SeekPosition 的存储值不应发生任何变化',
  );

  for (const e of afterEntries) {
    const target = readElementId(patched, afterSegStart + e.position);
    assert.equal(
      target.id, e.seekId,
      `改写后 SeekPosition ${e.position} 应仍指向 0x${e.seekId.toString(16)}，` +
      `实际 0x${target.id.toString(16)}`,
    );
  }
});

test('结构不变量：DocType 之后的字节逐位不变', () => {
  const raw = new Uint8Array(readFileSync(join(HERE, 'fixtures/tone-2s.webm')));
  const header = parseEbmlHeader(raw);
  const patched = patchBytes(raw, 'matroska');
  const r = buildPatchedHeader(raw);

  // 头部之后的全部数据（Segment 及其所有内容）必须逐字节相同
  const originalTail = raw.subarray(header.headerLen);
  const patchedTail = patched.subarray(r.header.length);
  assert.equal(patchedTail.length, originalTail.length);
  assert.ok(
    Buffer.from(patchedTail).equals(Buffer.from(originalTail)),
    '头部之后的字节必须完全不变 —— 改写只应触碰 EBML 头',
  );
});

test('fixture 的真实头部与金标准布局一致', () => {
  const raw = new Uint8Array(readFileSync(join(HERE, 'fixtures/tone-2s.webm')));
  const r = buildPatchedHeader(raw);
  assert.equal(r.oldDocType, 'webm', 'ffmpeg/libwebm 应写出 DocType=webm');
  assert.equal(r.oldHeaderLen, 36, '真实产物的头部应为 36 字节');
  assert.equal(hex(r.header), hex(EXPECTED_PATCHED), '真实产物的改写结果应与金标准一致');
});
