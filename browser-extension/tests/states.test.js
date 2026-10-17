// @ts-check
/**
 * 状态机的单测。
 *
 * 这些测试的价值在于：把「转移表以数据形式导出」这个设计决定变现 ——
 * 我们可以穷尽断言表本身的自洽性（没有指向不存在状态的边、终态可达等），
 * 而不必启动浏览器逐个手动触发状态。
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  S, ALL_STATES, LEGAL, ACTIVE_STATES,
  canTransition, assertTransition, isActive,
  E, isRetryable,
} from '../src/common/states.js';

test('转移表结构自洽：每个状态都有条目，且所有目标都是合法状态', () => {
  for (const state of ALL_STATES) {
    assert.ok(Array.isArray(LEGAL[state]), `状态 ${state} 缺少转移表条目`);
  }
  // 反向：表里不应有多余的键
  assert.deepEqual(
    Object.keys(LEGAL).sort(), [...ALL_STATES].sort(),
    '转移表的键应与状态枚举完全一致',
  );
  // 所有目标状态都必须是已声明的状态
  for (const [from, targets] of Object.entries(LEGAL)) {
    for (const to of targets) {
      assert.ok(
        ALL_STATES.includes(to),
        `${from} → ${to}：目标不是合法状态`,
      );
    }
  }
});

test('完整happy path 每一步都合法', () => {
  const path = [
    S.IDLE, S.ARMED, S.STARTING, S.RECORDING,
    S.STOPPING, S.FINALIZING, S.SAVING, S.DONE,
  ];
  for (let i = 0; i < path.length - 1; i += 1) {
    assert.ok(
      canTransition(path[i], path[i + 1]),
      `happy path 断裂: ${path[i]} → ${path[i + 1]}`,
    );
  }
});

test('done → starting 合法（停止捕获不撤销授权，「再次录制」是一等公民）', () => {
  assert.ok(canTransition(S.DONE, S.STARTING));
});

test('★ recording → recording 合法（进度/活性心跳以同状态上报）', () => {
  // 这条曾缺失：心跳每次都被 setState 拒绝，bytes 永远停在 0。
  // 症状隐形（bytes 为 0 时 UI 干脆不显示），靠这条断言防回归。
  assert.ok(canTransition(S.RECORDING, S.RECORDING));
});

test('error → starting 合法（授权仍在时允许重试）', () => {
  assert.ok(canTransition(S.ERROR, S.STARTING));
});

test('任何状态都能掉回 idle（跨源跳转 / 扩展重载会撤销授权）', () => {
  for (const state of ALL_STATES) {
    assert.ok(
      canTransition(state, S.IDLE),
      `${state} 应能转移到 idle —— 授权随时可能被浏览器撤销`,
    );
  }
});

test('几个应当非法的转移确实被拒绝', () => {
  // 未授权不能直接开录
  assert.ok(!canTransition(S.IDLE, S.STARTING), 'idle 不应能直接进入 starting');
  assert.ok(!canTransition(S.IDLE, S.RECORDING));
  // 不能跳过兑换 ack
  assert.ok(!canTransition(S.ARMED, S.RECORDING), 'armed 必须经由 starting');
  // 不能跳过收尾直接完成
  assert.ok(!canTransition(S.RECORDING, S.DONE), 'recording 必须经由 stopping/finalizing');
  assert.ok(!canTransition(S.RECORDING, S.SAVING));
  // 已完成的不能倒回录制中
  assert.ok(!canTransition(S.DONE, S.RECORDING));
});

test('assertTransition 对非法转移抛错', () => {
  assert.equal(assertTransition(S.ARMED, S.STARTING), S.STARTING);
  assert.throws(() => assertTransition(S.IDLE, S.RECORDING), /非法状态转移/);
});

test('从 idle 出发所有状态可达（没有孤岛）', () => {
  const seen = new Set([S.IDLE]);
  const queue = [S.IDLE];
  while (queue.length > 0) {
    const cur = /** @type {import('../src/common/states.js').TabState} */ (queue.shift());
    for (const next of LEGAL[cur]) {
      if (!seen.has(next)) { seen.add(next); queue.push(next); }
    }
  }
  const unreachable = ALL_STATES.filter((s) => !seen.has(s));
  assert.deepEqual(unreachable, [], `存在不可达状态: ${unreachable.join(', ')}`);
});

test('每个非 idle 状态都能回到 idle 或 done（没有死锁）', () => {
  // 反向可达性：从每个状态出发，必须能走到某个终态
  const terminal = new Set([S.IDLE, S.DONE]);
  for (const start of ALL_STATES) {
    const seen = new Set([start]);
    const queue = [start];
    let ok = terminal.has(start);
    while (queue.length > 0 && !ok) {
      const cur = /** @type {import('../src/common/states.js').TabState} */ (queue.shift());
      for (const next of LEGAL[cur]) {
        if (terminal.has(next)) { ok = true; break; }
        if (!seen.has(next)) { seen.add(next); queue.push(next); }
      }
    }
    assert.ok(ok, `状态 ${start} 无法到达任何终态 —— 存在死锁`);
  }
});

test('isActive 正确标记占用捕获槽位的状态', () => {
  assert.ok(isActive(S.RECORDING));
  assert.ok(isActive(S.STARTING));
  assert.ok(isActive(S.STOPPING));
  assert.ok(isActive(S.FINALIZING));
  assert.ok(isActive(S.SAVING));
  // 这些不占槽位
  assert.ok(!isActive(S.IDLE));
  assert.ok(!isActive(S.ARMED));
  assert.ok(!isActive(S.DONE));
  assert.ok(!isActive(S.ERROR));
});

test('ACTIVE_STATES 与 isActive 一致', () => {
  for (const s of ALL_STATES) {
    assert.equal(isActive(s), ACTIVE_STATES.includes(s), `${s} 的 isActive 判定不一致`);
  }
});

test('错误码全部唯一', () => {
  const values = Object.values(E);
  assert.equal(new Set(values).size, values.length, '存在重复的错误码');
  for (const v of values) {
    assert.ok(v.startsWith('E_'), `错误码 ${v} 应以 E_ 开头`);
  }
});

test('GRANT_LOST 不可重试（重试只会再失败，应提示用户重新授权）', () => {
  assert.equal(isRetryable(E.GRANT_LOST), false);
  // 这些是可重试的
  assert.equal(isRetryable(E.TAB_BUSY), true);
  assert.equal(isRetryable(E.STREAM_EXPIRED), true);
  assert.equal(isRetryable(E.DOWNLOAD), true);
  assert.equal(isRetryable(E.LOST), true);
});

test('PATCH_FAILED 不算可重试（已降级存为 .webm，数据没丢）', () => {
  // 语义澄清：EBML 改写失败时我们已经保存了未改写的 .webm，
  // 所以它不是「失败待重试」，而是「以降级格式成功」。
  assert.equal(isRetryable(E.PATCH_FAILED), false);
});
