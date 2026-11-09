// @ts-check
import test from 'node:test';
import assert from 'node:assert/strict';

import { T, shortcutsFrom } from '../src/ui/strings.js';

test('shortcutsFrom 取实际绑定，不假设 manifest 的建议键生效', () => {
  const got = shortcutsFrom([
    { name: '_execute_action', shortcut: '' },
    { name: 'arm-current-tab', shortcut: '⌥⇧Y' },
    { name: 'toggle-record-all', shortcut: '⌥⇧U' },
  ]);
  assert.deepEqual(got, { arm: '⌥⇧Y', toggle: '⌥⇧U' });
});

test('shortcutsFrom：被占用时 Chrome 返回空 shortcut，缺失的命令同样视为未绑定', () => {
  assert.deepEqual(
    shortcutsFrom([{ name: 'arm-current-tab', shortcut: '' }]),
    { arm: '', toggle: '' },
  );
});

test('授权提示在快捷键未绑定时只给出可用的途径', () => {
  assert.equal(T.hintArmThisTab(''), '切到该标签页，点击扩展图标授权');
  assert.match(T.hintArmThisTab('⌥⇧R'), /扩展图标或按 ⌥⇧R/);
});
