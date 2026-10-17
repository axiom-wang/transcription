// @ts-check
/**
 * 最小 chrome API 桩 —— 让 service worker 能在 Node 里被真实加载。
 *
 * ## 为什么值得做这个
 *
 * 开发环境里没有 Chrome，而 MV3 有一整类**加载期就炸掉**的错误：
 * 顶层调用了不存在的 API、监听器注册顺序不对、模块 import 失败。
 * 这类错误的症状是「扩展装上了，但点什么都没反应」—— 最难排查的一种。
 *
 * 用桩把 service-worker.js 真正 import 一次，就能在 CI 里拦住它们。
 *
 * 桩只记录调用、返回合理的空值，不模拟浏览器语义 ——
 * 目的是验证「代码能跑起来、调用顺序对」，不是模拟 Chrome。
 */

/**
 * @typedef {object} Call
 * @property {string} api
 * @property {any[]} args
 */

export function createChromeStub() {
  /** @type {Call[]} */
  const calls = [];
  /** @type {Record<string, Function[]>} */
  const listeners = {};

  /** @param {string} api @param {any[]} args */
  const record = (api, ...args) => { calls.push({ api, args }); };

  /** @param {string} name */
  const event = (name) => {
    listeners[name] = [];
    return {
      /** @param {Function} fn */
      addListener: (fn) => { listeners[name].push(fn); },
      /** @param {Function} fn */
      removeListener: (fn) => {
        listeners[name] = listeners[name].filter((f) => f !== fn);
      },
      hasListener: (/** @type {Function} */ fn) => listeners[name].includes(fn),
    };
  };

  /** session storage 的内存后端 */
  /** @type {Record<string, any>} */
  const sessionData = {};
  /** @type {Record<string, any>} */
  const localData = {};

  const chrome = {
    runtime: {
      id: 'stub-extension-id',
      lastError: undefined,
      onInstalled: event('runtime.onInstalled'),
      onStartup: event('runtime.onStartup'),
      onMessage: event('runtime.onMessage'),
      getContexts: async (/** @type {any} */ filter) => {
        record('runtime.getContexts', filter);
        return [];
      },
      sendMessage: async (/** @type {any} */ msg) => {
        record('runtime.sendMessage', msg);
        return { ok: true };
      },
      sendNativeMessage: async (host, message) => {
        record('runtime.sendNativeMessage', host, message);
        return { ok: true, reused: false, request: 'test-request', name: '录音.mkv' };
      },
      getURL: (/** @type {string} */ p) => `chrome-extension://stub/${p}`,
    },

    action: {
      onClicked: event('action.onClicked'),
      setBadgeText: async (/** @type {any} */ o) => { record('action.setBadgeText', o); },
      setBadgeBackgroundColor: async (/** @type {any} */ o) => {
        record('action.setBadgeBackgroundColor', o);
      },
    },

    commands: { onCommand: event('commands.onCommand') },

    contextMenus: {
      onClicked: event('contextMenus.onClicked'),
      removeAll: (/** @type {Function} */ cb) => { record('contextMenus.removeAll'); cb?.(); },
      create: (/** @type {any} */ o) => { record('contextMenus.create', o); },
    },

    tabs: {
      TAB_ID_NONE: -1,
      onUpdated: event('tabs.onUpdated'),
      onRemoved: event('tabs.onRemoved'),
      onReplaced: event('tabs.onReplaced'),
      get: async (/** @type {number} */ id) => {
        record('tabs.get', id);
        return { id, url: 'https://example.com/', title: '示例页面', windowId: 1, audible: true };
      },
      query: async (/** @type {any} */ q) => { record('tabs.query', q); return []; },
      update: async (/** @type {number} */ id, /** @type {any} */ o) => {
        record('tabs.update', id, o);
        return { id, windowId: 1 };
      },
    },

    windows: {
      update: async (/** @type {number} */ id, /** @type {any} */ o) => {
        record('windows.update', id, o);
      },
    },

    storage: {
      session: {
        get: async (/** @type {string[]} */ keys) => {
          record('storage.session.get', keys);
          if (!keys) return { ...sessionData };
          const out = /** @type {Record<string, any>} */ ({});
          for (const k of [].concat(/** @type {any} */ (keys))) {
            if (k in sessionData) out[k] = sessionData[k];
          }
          return out;
        },
        set: async (/** @type {Record<string, any>} */ obj) => {
          record('storage.session.set', obj);
          Object.assign(sessionData, obj);
        },
        onChanged: event('storage.session.onChanged'),
      },
      local: {
        get: async () => ({ ...localData }),
        set: async (/** @type {Record<string, any>} */ obj) => {
          record('storage.local.set', obj);
          Object.assign(localData, obj);
        },
      },
    },

    sidePanel: {
      // 返回 Promise —— 真实 API 在非用户手势中调用时会 reject，
      // 桩不模拟该行为，由测试自己断言调用时机。
      open: async (/** @type {any} */ o) => { record('sidePanel.open', o); },
      setOptions: async (/** @type {any} */ o) => { record('sidePanel.setOptions', o); },
    },

    offscreen: {
      createDocument: async (/** @type {any} */ o) => { record('offscreen.createDocument', o); },
      closeDocument: async () => { record('offscreen.closeDocument'); },
      hasDocument: async () => false,
    },

    tabCapture: {
      getMediaStreamId: async (/** @type {any} */ o) => {
        record('tabCapture.getMediaStreamId', o);
        return 'stub-stream-id';
      },
      getCapturedTabs: async () => [],
    },

    downloads: {
      onChanged: event('downloads.onChanged'),
      search: async (query) => { record('downloads.search', query); return []; },
      download: (/** @type {any} */ o, /** @type {Function} */ cb) => {
        record('downloads.download', o);
        cb?.(1);
      },
    },
  };

  return {
    chrome,
    calls,
    listeners,
    /** @param {string} api */
    callsTo: (api) => calls.filter((c) => c.api === api),
    /** @param {string} name */
    fire: (name, /** @type {any[]} */ ...args) =>
      (listeners[name] ?? []).map((fn) => fn(...args)),
    reset: () => { calls.length = 0; },
  };
}

/**
 * offscreen 文档里**真实存在**的 chrome API 白名单。
 *
 * 官方文档明确写了两次：「`runtime` API 是 offscreen 文档唯一支持的扩展 API」。
 * 权限声明照样生效，但 API 面被刻意收窄了。
 *
 * ⚠️ 这份白名单是一个真实故障的产物：saver.js 曾在 offscreen 里调
 * `chrome.downloads.onChanged`，线上报 "Cannot read properties of undefined
 * (reading 'onChanged')" —— 每一次录制的保存都失败。
 * 而当时的桩在所有上下文都提供了完整的 chrome，所以测试全绿、完全没拦住。
 */
const OFFSCREEN_ALLOWED_APIS = ['runtime'];

/**
 * 把桩装到 globalThis 上。必须在 import 被测模块**之前**调用。
 *
 * @param {object} [opts]
 * @param {'sw'|'offscreen'|'panel'} [opts.context]
 *   模拟哪个上下文。传 `'offscreen'` 会**移除** runtime 以外的所有 API，
 *   从而在测试里复现真实的上下文限制。默认 `'sw'`（完整 API）。
 * @returns {ReturnType<typeof createChromeStub>}
 */
export function installChromeStub(opts = {}) {
  const stub = createChromeStub();

  if (opts.context === 'offscreen') {
    for (const key of Object.keys(stub.chrome)) {
      if (!OFFSCREEN_ALLOWED_APIS.includes(key)) {
        // 删掉而不是设为 undefined —— 真实环境里这些命名空间根本不存在，
        // 访问 chrome.downloads.onChanged 会抛 TypeError，正是我们要复现的。
        delete /** @type {any} */ (stub.chrome)[key];
      }
    }
  }

  // @ts-ignore —— 故意往 global 上挂 chrome
  globalThis.chrome = stub.chrome;
  return stub;
}
