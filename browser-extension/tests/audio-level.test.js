// @ts-check
/**
 * 音频活性检测的纯逻辑测试。
 *
 * 这个模块守护的产品承诺是：默认静默录制下，面板的「有声 / 静音」指示
 * 是用户确认「真的在录」的唯一途径。所以两个方向的误判都要拦：
 * - 把真实播放误判为静音（吓用户的假警告）—— 更不可接受
 * - 把全程静音显示成有声（录了两小时白录）—— 同样不可接受
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  SILENCE_RMS_THRESHOLD,
  NO_SOUND_WARN_MS,
  SILENT_INDICATOR_MS,
  computeRms,
  LevelTracker,
} from '../src/common/audio-level.js';

// ───────────────────────────── computeRms ─────────────────────────────

test('computeRms：空输入与全零都是 0', () => {
  assert.equal(computeRms(new Float32Array(0)), 0);
  assert.equal(computeRms(new Float32Array(256)), 0);
});

test('computeRms：恒定信号等于其绝对值', () => {
  const buf = new Float32Array(100).fill(0.5);
  assert.ok(Math.abs(computeRms(buf) - 0.5) < 1e-6);
});

test('computeRms：已知值', () => {
  // [0.3, 0.4] → sqrt((0.09 + 0.16) / 2) = sqrt(0.125)
  const rms = computeRms(new Float32Array([0.3, 0.4]));
  assert.ok(Math.abs(rms - Math.sqrt(0.125)) < 1e-6);
});

test('阈值的量级合理：数字静音远低于它，1% 音量远高于它', () => {
  // 这两条是「阈值不会双向误判」的不变量，改阈值时必须先过这关
  assert.ok(computeRms(new Float32Array(2048)) < SILENCE_RMS_THRESHOLD, '数字静音必须低于阈值');
  assert.ok(
    computeRms(new Float32Array(2048).fill(0.01)) > SILENCE_RMS_THRESHOLD,
    '1% 音量必须高于阈值',
  );
});

// ───────────────────────────── LevelTracker ─────────────────────────────

/** 造一个带假时钟的 tracker，返回时钟推进函数 */
function makeTracker(startAt = 1_000_000) {
  let now = startAt;
  return {
    tracker: new LevelTracker({ now: () => now }),
    /** @param {number} ms */
    advance(ms) { now += ms; },
  };
}

test('未采样时 silentMs 为 0（不该误报）', () => {
  const { tracker } = makeTracker();
  assert.equal(tracker.silentMs, 0);
  assert.equal(tracker.hasSound, false);
});

test('持续静音：silentMs 自首次采样起累积，hasSound 保持 false', () => {
  const { tracker, advance } = makeTracker();
  tracker.update(0);
  advance(NO_SOUND_WARN_MS + 1);
  tracker.update(0.0001); // 低于阈值

  assert.equal(tracker.hasSound, false);
  assert.ok(
    tracker.silentMs > NO_SOUND_WARN_MS,
    '录制开始后持续无声超过阈值 —— 面板应显示「未检测到声音」警告',
  );
});

test('有声：hasSound 置位，silentMs 归零', () => {
  const { tracker, advance } = makeTracker();
  tracker.update(0);
  advance(5000);
  tracker.update(0.02); // 明显高于阈值

  assert.equal(tracker.hasSound, true);
  assert.equal(tracker.silentMs, 0);
});

test('★ 有声之后转入安静：不触发无声警告（演讲间隙是正常内容）', () => {
  const { tracker, advance } = makeTracker();
  tracker.update(0.02); // 先有过声音
  advance(NO_SOUND_WARN_MS + 5000);
  tracker.update(0);

  assert.equal(tracker.hasSound, true, 'hasSound 是「曾经有声」的锁存，不回退');
  assert.ok(tracker.silentMs > SILENT_INDICATOR_MS, '指示应显示「静音」');
  // 警告的判定（UI 侧）是 !hasSound && silentMs > NO_SOUND_WARN_MS —— 这里 hasSound 为 true，不警告
});

test('静音后再次有声：silentMs 重新归零', () => {
  const { tracker, advance } = makeTracker();
  tracker.update(0.02);
  advance(8000);
  tracker.update(0);
  assert.ok(tracker.silentMs >= 8000);
  tracker.update(0.03);
  assert.equal(tracker.silentMs, 0);
});

test('阈值边界：恰好等于阈值算有声', () => {
  const { tracker } = makeTracker();
  tracker.update(SILENCE_RMS_THRESHOLD);
  assert.equal(tracker.hasSound, true, '边界值应判为有声 —— 宁可误报有声，不可误报静音');
});

test('★ snapshot 全是有限数值，可 JSON 序列化（要经 storage 镜像到 panel）', () => {
  // Infinity / NaN 经 JSON 会变成 null，panel 端的数值比较会得到荒谬结果。
  // 这条测试守着「快照可安全进 chrome.storage」这个隐含契约。
  const { tracker, advance } = makeTracker();
  const before = tracker.snapshot();
  tracker.update(0);
  advance(3000);
  const after = tracker.snapshot();

  for (const snap of [before, after]) {
    assert.deepEqual(
      JSON.parse(JSON.stringify(snap)), snap,
      '快照经 JSON 往返后必须不变（无 Infinity / NaN / undefined）',
    );
  }
});
