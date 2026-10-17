// @ts-check
/**
 * offscreen 文档的 chrome API 限制测试。
 *
 * ## 这个文件的来由
 *
 * 线上故障：每一次录制的保存都失败，报
 * `Cannot read properties of undefined (reading 'onChanged')`。
 *
 * 根因：`saver.js` 在 offscreen 文档里调 `chrome.downloads.onChanged`，
 * 但官方文档明确写了两次「`runtime` API 是 offscreen 文档唯一支持的扩展 API」——
 * 权限声明照样生效，API 面却被刻意收窄了。`chrome.downloads` 在那里是 undefined。
 *
 * 为什么原来的 80 项测试全绿却没拦住：桩在所有上下文都提供了完整的 `chrome`。
 * 桩比真实环境**宽松**，于是它掩盖了真实的约束 —— 这类"桩太友善"的问题，
 * 比没有测试更危险，因为它给了虚假的安全感。
 *
 * 现在 `installChromeStub({context:'offscreen'})` 会删掉 runtime 以外的一切，
 * 下面的测试就是在这个真实约束下跑的。
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { installChromeStub } from './helpers/chrome-stub.js';

test('★ offscreen 上下文里只有 chrome.runtime 可用', () => {
  const stub = installChromeStub({ context: 'offscreen' });
  const available = Object.keys(stub.chrome);

  assert.deepEqual(
    available, ['runtime'],
    `offscreen 只应有 runtime，实际有 ${available.join(', ')}`,
  );

  // 这些命名空间在 offscreen 里必须不存在 —— 碰它们就是那个线上故障
  for (const api of ['downloads', 'tabs', 'storage', 'action', 'tabCapture', 'offscreen']) {
    assert.equal(
      /** @type {any} */ (stub.chrome)[api], undefined,
      `chrome.${api} 在 offscreen 文档里不应存在`,
    );
  }
});

test('★ 桩能复现线上那条报错（防止桩再次比真实环境宽松）', () => {
  const stub = installChromeStub({ context: 'offscreen' });
  assert.throws(
    () => {
      // 这正是 saver.js 曾经做的事
      /** @type {any} */ (stub.chrome).downloads.onChanged.addListener(() => {});
    },
    /Cannot read properties of undefined \(reading 'onChanged'\)/,
    '桩必须能复现真实的 TypeError，否则它无法守住这个约束',
  );
});

test('★ offscreen 目录下的源码不得引用 runtime 以外的 chrome API', async () => {
  // 静态检查，比运行时测试更早、更彻底：只要有人在 offscreen 目录里写了
  // chrome.downloads / chrome.tabs / chrome.storage，这条就红。
  const { readFileSync, readdirSync } = await import('node:fs');
  const { fileURLToPath } = await import('node:url');
  const { dirname, join } = await import('node:path');

  const here = dirname(fileURLToPath(import.meta.url));
  const dir = join(here, '../src/offscreen');

  /** @type {string[]} */
  const violations = [];

  for (const name of readdirSync(dir)) {
    if (!name.endsWith('.js')) continue;
    const src = readFileSync(join(dir, name), 'utf8');

    // 去掉注释，避免把说明文字里的 chrome.downloads 当成真实调用
    const code = src
      .replace(/\/\*[\s\S]*?\*\//g, '')
      .replace(/\/\/[^\n]*/g, '');

    for (const m of code.matchAll(/chrome\.(\w+)/g)) {
      if (m[1] !== 'runtime') {
        violations.push(`${name}: chrome.${m[1]}`);
      }
    }
  }

  assert.deepEqual(
    violations, [],
    'offscreen 目录只能用 chrome.runtime；需要其他 API 时必须通过消息委托给 service worker。\n' +
    `发现：\n  ${violations.join('\n  ')}`,
  );
});
