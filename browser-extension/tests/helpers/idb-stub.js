// @ts-check
/**
 * 最小 IndexedDB 桩 —— 让 chunk-store.js 能在 Node 里被真实测试。
 *
 * ## 为什么手写而不用 fake-indexeddb
 *
 * 这个项目的原则是**零依赖**（连生产代码都不引入运行时依赖）。
 * chunk-store 只用到 IDB 的一个很窄的子集：open / transaction / objectStore /
 * put / get / getAll / delete / IDBKeyRange.bound。手写这些比引入一个包更可控，
 * 也不会因为 fake-indexeddb 的行为差异掩盖真实问题。
 *
 * ## 必须正确实现的一件事：键序
 *
 * chunk-store 用复合键 `[sessionId, tabId, seq]`，并依赖 IDB 的键序保证
 * `getAll(range)` 返回的分块**天然按 seq 有序**（拼接顺序正确）。
 * 而删除用的范围上界是 `[sessionId, tabId, []]` —— 依赖「数组排在数字之后」
 * 这条规则。所以这个桩必须实现真实的 IDB 键比较算法，否则测试会通过
 * 而真实环境失败，那比没有测试更糟。
 *
 * 键类型顺序（IndexedDB 规范）：数字 < 日期 < 字符串 < 二进制 < 数组。
 * 数组按元素逐个比较；相同前缀下**短数组排在长数组之前**。
 */

/**
 * IDB 键比较。返回 <0 / 0 / >0。
 * @param {any} a
 * @param {any} b
 * @returns {number}
 */
export function compareKeys(a, b) {
  const rank = (/** @type {any} */ v) => {
    if (Array.isArray(v)) return 4;
    if (v instanceof Date) return 1;
    if (typeof v === 'string') return 2;
    if (typeof v === 'number') return 0;
    return 3; // 二进制
  };
  const ra = rank(a);
  const rb = rank(b);
  if (ra !== rb) return ra - rb;

  if (Array.isArray(a) && Array.isArray(b)) {
    const n = Math.min(a.length, b.length);
    for (let i = 0; i < n; i += 1) {
      const c = compareKeys(a[i], b[i]);
      if (c !== 0) return c;
    }
    return a.length - b.length; // 短数组在前
  }
  if (a < b) return -1;
  if (a > b) return 1;
  return 0;
}

/** 简易 IDBKeyRange */
class FakeKeyRange {
  /**
   * @param {any} lower @param {any} upper
   * @param {boolean} [lowerOpen] @param {boolean} [upperOpen]
   */
  constructor(lower, upper, lowerOpen = false, upperOpen = false) {
    this.lower = lower;
    this.upper = upper;
    this.lowerOpen = lowerOpen;
    this.upperOpen = upperOpen;
  }

  /** @param {any} key */
  includes(key) {
    if (this.lower !== undefined) {
      const c = compareKeys(key, this.lower);
      if (c < 0 || (c === 0 && this.lowerOpen)) return false;
    }
    if (this.upper !== undefined) {
      const c = compareKeys(key, this.upper);
      if (c > 0 || (c === 0 && this.upperOpen)) return false;
    }
    return true;
  }
}

/** 一个简易请求对象，模拟 IDBRequest 的 onsuccess/onerror 时序 */
/**
 * @param {() => any} compute
 */
function makeRequest(compute) {
  /** @type {any} */
  const req = { result: undefined, error: null, onsuccess: null, onerror: null };
  // 用 microtask 模拟异步回调，保证 onsuccess 在设置之后才触发
  queueMicrotask(() => {
    try {
      req.result = compute();
      req.onsuccess?.();
    } catch (err) {
      req.error = err;
      req.onerror?.();
    }
  });
  return req;
}

class FakeObjectStore {
  /**
   * @param {Map<string, any>} data 键的 JSON 序列 → 记录
   * @param {string[]} keyPath
   * @param {FakeTransaction} tx
   */
  constructor(data, keyPath, tx) {
    this.data = data;
    this.keyPath = keyPath;
    this.tx = tx;
  }

  /** @param {any} record */
  keyOf(record) {
    return this.keyPath.map((p) => record[p]);
  }

  /** @param {any} record */
  put(record) {
    this.tx.assertWritable();
    const key = this.keyOf(record);
    return makeRequest(() => {
      this.data.set(JSON.stringify(key), { key, value: record });
      return key;
    });
  }

  /** @param {any} key */
  get(key) {
    const wanted = Array.isArray(key) ? key : [key];
    return makeRequest(() => {
      for (const entry of this.data.values()) {
        if (compareKeys(entry.key, wanted) === 0) return entry.value;
      }
      return undefined;
    });
  }

  /** @param {FakeKeyRange} [range] */
  getAll(range) {
    return makeRequest(() => {
      const rows = Array.from(this.data.values())
        .filter((e) => !range || range.includes(e.key))
        .sort((a, b) => compareKeys(a.key, b.key)); // ← 真实 IDB 的键序
      return rows.map((e) => e.value);
    });
  }

  /** @param {any} keyOrRange */
  delete(keyOrRange) {
    this.tx.assertWritable();
    return makeRequest(() => {
      for (const [k, entry] of Array.from(this.data.entries())) {
        const hit = keyOrRange instanceof FakeKeyRange
          ? keyOrRange.includes(entry.key)
          : compareKeys(entry.key, Array.isArray(keyOrRange) ? keyOrRange : [keyOrRange]) === 0;
        if (hit) this.data.delete(k);
      }
      return undefined;
    });
  }
}

class FakeTransaction {
  /**
   * @param {FakeDb} db @param {string[]} names @param {string} mode
   */
  constructor(db, names, mode) {
    this.db = db;
    this.names = names;
    this.mode = mode;
    this.error = null;
    /** @type {Function|null} */
    this.oncomplete = null;
    /** @type {Function|null} */
    this.onerror = null;
    /** @type {Function|null} */
    this.onabort = null;

    // 事务在当前微任务队列清空后完成 —— 足以让同步发起的请求先跑完
    queueMicrotask(() => {
      queueMicrotask(() => {
        queueMicrotask(() => this.oncomplete?.());
      });
    });
  }

  assertWritable() {
    if (this.mode !== 'readwrite') {
      throw new Error('在只读事务里写入');
    }
  }

  /** @param {string} name */
  objectStore(name) {
    const store = this.db.stores.get(name);
    if (!store) throw new Error(`不存在的表: ${name}`);
    return new FakeObjectStore(store.data, store.keyPath, this);
  }
}

class FakeDb {
  constructor() {
    /** @type {Map<string, {data:Map<string,any>, keyPath:string[]}>} */
    this.stores = new Map();
    this.objectStoreNames = {
      /** @param {string} n */
      contains: (n) => this.stores.has(n),
    };
    /** @type {Function|null} */
    this.onversionchange = null;
  }

  /**
   * @param {string} name
   * @param {{keyPath: string|string[]}} opts
   */
  createObjectStore(name, opts) {
    const keyPath = Array.isArray(opts.keyPath) ? opts.keyPath : [opts.keyPath];
    this.stores.set(name, { data: new Map(), keyPath });
    return new FakeObjectStore(this.stores.get(name).data, keyPath, /** @type {any} */ ({
      assertWritable: () => {},
    }));
  }

  /**
   * @param {string[]|string} names @param {string} mode
   */
  transaction(names, mode = 'readonly') {
    return new FakeTransaction(this, Array.isArray(names) ? names : [names], mode);
  }

  close() {}
}

/**
 * 安装 indexedDB 桩到 globalThis。必须在 import chunk-store 之前调用。
 *
 * @returns {{db: FakeDb, reset: () => void}}
 */
export function installIndexedDbStub() {
  const db = new FakeDb();

  // @ts-ignore
  globalThis.indexedDB = {
    /** @param {string} _name @param {number} _version */
    open(_name, _version) {
      /** @type {any} */
      const req = {
        result: db, error: null,
        onsuccess: null, onerror: null, onupgradeneeded: null,
      };
      queueMicrotask(() => {
        // 首次打开触发 upgradeneeded，让 chunk-store 建表
        if (db.stores.size === 0) req.onupgradeneeded?.();
        req.onsuccess?.();
      });
      return req;
    },
  };

  // @ts-ignore
  globalThis.IDBKeyRange = {
    /**
     * @param {any} lower @param {any} upper
     * @param {boolean} [lowerOpen] @param {boolean} [upperOpen]
     */
    bound: (lower, upper, lowerOpen, upperOpen) =>
      new FakeKeyRange(lower, upper, lowerOpen, upperOpen),
    /** @param {any} v */
    only: (v) => new FakeKeyRange(v, v),
  };

  return {
    db,
    reset: () => {
      for (const s of db.stores.values()) s.data.clear();
    },
  };
}
