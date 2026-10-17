// @ts-check
/**
 * 文件名净化的单测。重点覆盖 CJK 字节截断和 downloads API 会直接拒绝的那些形式。
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  TITLE_BYTE_LIMIT,
  utf8Length,
  truncateUtf8,
  sanitizeSegment,
  formatSessionStamp,
  formatTimeStamp,
  buildDownloadPath,
  buildAsciiFallbackPath,
} from '../src/common/filename.js';

test('路径分隔符必须被替换（否则会写到意外的目录）', () => {
  // 这是最危险的一条：未处理的 / 会被 chrome.downloads 当成目录分隔符
  assert.equal(sanitizeSegment('a/b'), 'a_b');
  assert.equal(sanitizeSegment('a\\b'), 'a_b');
  // 遍历路径的尝试：/ 被替换为 _，且开头的点被步骤 ⑤ 去掉。
  // 关键安全性质是「结果里不含任何分隔符」，而非具体长什么样。
  assert.equal(sanitizeSegment('../../etc/passwd'), '_.._etc_passwd');
  assert.ok(!sanitizeSegment('前进/后退').includes('/'));
});

test('Windows 非法字符被替换', () => {
  assert.equal(sanitizeSegment('a<b>c:d"e|f?g*h'), 'a_b_c_d_e_f_g_h');
});

test('控制字符被剥离', () => {
  assert.equal(sanitizeSegment('a\x00b\x1fc\x7fd'), 'abcd');
  assert.equal(sanitizeSegment('标题\n换行\t制表'), '标题 换行 制表');
});

test('空白折叠与 trim', () => {
  assert.equal(sanitizeSegment('  a   b  '), 'a b');
  // 全角空格也算空白
  assert.equal(sanitizeSegment('a　b'), 'a b');
});

test('首尾的点和空格被去掉（Windows 会静默丢弃）', () => {
  assert.equal(sanitizeSegment('...abc...'), 'abc');
  assert.equal(sanitizeSegment('  .abc.  '), 'abc');
  // 中间的点要保留
  assert.equal(sanitizeSegment('a.b.c'), 'a.b.c');
});

test('Windows 保留设备名加前缀（带扩展名时同样保留）', () => {
  assert.equal(sanitizeSegment('CON'), '_CON');
  assert.equal(sanitizeSegment('con'), '_con');
  assert.equal(sanitizeSegment('COM1'), '_COM1');
  assert.equal(sanitizeSegment('LPT9'), '_LPT9');
  // CON.mkv 在 Windows 上仍是保留名 —— 判断时必须去掉扩展名
  assert.equal(sanitizeSegment('CON.mkv'), '_CON.mkv');
  // 但 CONSOLE 不是保留名
  assert.equal(sanitizeSegment('CONSOLE'), 'CONSOLE');
});

test('净化后为空时使用兜底名', () => {
  assert.equal(sanitizeSegment('', { fallback: '标签页-42' }), '标签页-42');
  assert.equal(sanitizeSegment('...', { fallback: '标签页-42' }), '标签页-42');
  assert.equal(sanitizeSegment('\x00\x01', { fallback: '标签页-42' }), '标签页-42');
  assert.equal(sanitizeSegment('   ', { fallback: '标签页-42' }), '标签页-42');
});

// ─────────────────── CJK 字节截断（最容易出错的一条） ───────────────────

test('utf8Length: 汉字占 3 字节', () => {
  assert.equal(utf8Length('abc'), 3);
  assert.equal(utf8Length('中文'), 6);
  assert.equal(utf8Length('中'), 3);
});

test('按字节截断：100 个汉字会超出 120 字节上限', () => {
  const title = '标'.repeat(100); // 300 字节
  const out = sanitizeSegment(title);
  assert.ok(utf8Length(out) <= TITLE_BYTE_LIMIT, `实际 ${utf8Length(out)} 字节`);
  // 120 / 3 = 40 个汉字
  assert.equal(out.length, 40);
});

test('按字节截断不劈开 emoji 代理对', () => {
  // 每个 😀 是 4 字节、2 个 UTF-16 码元。若按字符截断会产生半个代理对。
  const s = '😀'.repeat(40); // 160 字节
  const out = truncateUtf8(s, 30);
  assert.ok(utf8Length(out) <= 30);
  // 不应含孤立代理项
  assert.ok(!/[\uD800-\uDFFF]/.test(out.replace(/[\uD800-\uDBFF][\uDC00-\uDFFF]/g, '')),
    '不应留下孤立的代理项');
  assert.equal(out, '😀'.repeat(7)); // 7*4 = 28 ≤ 30
});

test('按字节截断不劈开 ZWJ 家庭 emoji', () => {
  // 👨‍👩‍👧‍👦 是 4 个 emoji 用 ZWJ 连接，共 25 字节。切一半会变成散开的人像。
  const family = '👨‍👩‍👧‍👦';
  assert.equal(utf8Length(family), 25);
  // 上限 30 只放得下一个完整家庭
  const out = truncateUtf8(family + family, 30);
  assert.equal(out, family, '应保留一个完整的 ZWJ 序列，而不是切开第二个');
});

test('截断后不会在末尾留下空格或点', () => {
  // 构造一个恰好在空格处被切断的标题
  const s = 'a'.repeat(118) + ' bcd';
  const out = sanitizeSegment(s);
  assert.ok(!out.endsWith(' '), `不应以空格结尾: ${JSON.stringify(out.slice(-5))}`);
  assert.ok(!out.endsWith('.'));
});

test('NFC 归一化：分解形式与合成形式产生相同结果', () => {
  const composed = 'é';                    // U+00E9
  const decomposed = 'é';            // e + 组合锐音符
  assert.notEqual(composed, decomposed, '两者的码点序列本应不同');
  assert.equal(sanitizeSegment(composed), sanitizeSegment(decomposed),
    'NFC 归一化后应产生一致的文件名');
});

test('短标题不受影响', () => {
  assert.equal(sanitizeSegment('正常的视频标题'), '正常的视频标题');
  assert.equal(sanitizeSegment('Normal Title'), 'Normal Title');
});

// ───────────────────────────── 路径组装 ─────────────────────────────

test('formatSessionStamp / formatTimeStamp 补零正确', () => {
  const d = new Date(2026, 7, 28, 9, 5, 3); // 2026-08-28 09:05:03
  assert.equal(formatSessionStamp(d), '2026-08-28_09-05');
  assert.equal(formatTimeStamp(d), '090503');
});

test('buildDownloadPath 组装完整相对路径', () => {
  const p = buildDownloadPath({
    title: '某个视频标题',
    index: 1,
    sessionStamp: '2026-08-28_14-30',
    stoppedAt: new Date(2026, 7, 28, 14, 30, 52),
    tabId: 7,
  });
  assert.equal(p, '标签页录音/2026-08-28_14-30/01-某个视频标题-143052.mkv');
});

test('buildDownloadPath: 恶意标题不会逃出会话目录', () => {
  const p = buildDownloadPath({
    title: '../../../../evil',
    index: 2,
    sessionStamp: '2026-08-28_14-30',
    stoppedAt: new Date(2026, 7, 28, 14, 30, 52),
    tabId: 7,
  });
  // 只允许两个分隔符（root/session/file），标题里的 / 已被替换
  assert.equal(p.split('/').length, 3, `路径段数异常: ${p}`);
  assert.ok(p.startsWith('标签页录音/2026-08-28_14-30/'));
  assert.ok(!p.includes('..' + '/'));
});

test('buildDownloadPath: 空标题用 tabId 兜底', () => {
  const p = buildDownloadPath({
    title: '   ',
    index: 3,
    sessionStamp: '2026-08-28_14-30',
    stoppedAt: new Date(2026, 7, 28, 14, 30, 52),
    tabId: 99,
  });
  assert.ok(p.includes('标签页-99'), p);
});

test('buildDownloadPath: 整条路径长度受控（含超长 CJK 标题）', () => {
  const p = buildDownloadPath({
    title: '标'.repeat(200),
    index: 1,
    sessionStamp: '2026-08-28_14-30',
    stoppedAt: new Date(2026, 7, 28, 14, 30, 52),
    tabId: 7,
  });
  // 单个路径段都必须在 255 字节以内
  for (const seg of p.split('/')) {
    assert.ok(utf8Length(seg) < 255, `路径段过长（${utf8Length(seg)} 字节）: ${seg}`);
  }
});

test('buildAsciiFallbackPath 只含 ASCII', () => {
  const p = buildAsciiFallbackPath({
    index: 4,
    sessionStamp: '2026-08-28_14-30',
    stoppedAt: new Date(2026, 7, 28, 14, 30, 52),
  });
  assert.equal(p, 'tab-recordings/2026-08-28_14-30/04-rec-143052.mkv');
  // eslint-disable-next-line no-control-regex
  assert.ok(/^[\x20-\x7e]+$/.test(p), `应只含可打印 ASCII: ${p}`);
});

test('扩展名可切换（EBML 改写失败时降级为 .webm）', () => {
  const p = buildDownloadPath({
    title: '标题',
    index: 1,
    sessionStamp: '2026-08-28_14-30',
    stoppedAt: new Date(2026, 7, 28, 14, 30, 52),
    tabId: 7,
    extension: 'webm',
  });
  assert.ok(p.endsWith('.webm'), p);
});
