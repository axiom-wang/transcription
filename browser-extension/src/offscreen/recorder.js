// @ts-check
/**
 * TabRecorder —— 单个 Tab 的捕获、录制、回放监听与收尾。
 *
 * ## ★ 最重要的一条：录原始流，监听另分一路
 *
 * `tabCapture` 会**静音该 Tab 的本地播放**，所以必须把流接回
 * `AudioContext.destination` 用户才能继续听到声音。但：
 *
 *   ✅ MediaRecorder ← 原始 MediaStream
 *   ✅ AudioContext  ← 另一条 MediaStreamAudioSourceNode（回放 + 活性分析）
 *   ❌ MediaRecorder ← AudioContext 的输出
 *
 * 若把录制也走 AudioContext，就会引入**静默的重采样**：tabCapture 是 48kHz，
 * 而 AudioContext 可能默认 44.1kHz，于是每个文件都被悄悄重采样一遍。
 * 这个错误不会报错、不会崩溃，只会让所有输出音质变差 —— 极难事后发现。
 * 同一个 MediaStream 被两个消费者共享是完全安全的。
 *
 * ## 活性分析：链路常驻，增益开关
 *
 * 分析链路（source → analyser → gain → destination）**始终建立**，
 * 与 monitor 开关无关 —— 默认静默录制时用户听不到声音，面板上的
 * 「有声 / 静音」指示就成了确认「真的在录」的唯一途径，这正是最需要
 * 它的场景。monitor 只控制 GainNode 的增益：0 静音、1 回放。
 * AnalyserNode 只读数据、不改变信号，对录制零影响。
 *
 * ## 尾块保障（三重保险）
 *
 * Tab 关闭时 `MediaRecorder` **不会**可靠地自动收尾，最后一个分块会丢，
 * 甚至产生无法播放的容器。所以：
 * 1. 主路径：track 的 `ended` 事件 → requestData() → stop()
 * 2. 第二重：SW 侧 `tabs.onRemoved` → 发 STOP_ONE
 * 3. 第三重：1 秒轮询 `track.readyState`，兜住事件完全没触发的情况
 *
 * 另外 `tabCapture.onStatusChanged` 在 Tab 关闭时**可能根本不触发**
 * （KillRequest 不 dispatch 事件），所以我们完全不依赖它。
 */

import { S, E } from '../common/states.js';
import { createLogger } from '../common/log.js';
import { LevelTracker, computeRms } from '../common/audio-level.js';
import * as store from '../common/chunk-store.js';

const log = createLogger('recorder');

/** 首选 MIME。刻意用 audio/webm 而非 audio/x-matroska —— 后者只是别名，
 *  产出的字节 DocType 仍是 "webm"，反而会让人误以为拿到了 MKV。
 *  真正的 Matroska 由 mkv.js 改写 EBML 头得到。 */
const PREFERRED_MIME = 'audio/webm;codecs=opus';
const FALLBACK_MIMES = ['audio/webm', ''];

/** onstop 看门狗：超时就用已有分块抢救，而不是永久挂起 */
const STOP_WATCHDOG_MS = 5000;
/** track.readyState 轮询间隔（第三重保险） */
const TRACK_POLL_MS = 1000;

/** 共享的 AudioContext —— N 个 Tab 共用一个，比 N 个 context 便宜得多 */
/** @type {AudioContext | null} */
let sharedContext = null;

/**
 * 取得（并在必要时唤醒）共享 AudioContext。
 *
 * offscreen 文档永远收不到用户手势，而自动播放策略规定「无手势时创建的
 * AudioContext 处于 suspended」。官方示例依赖这条路径能工作，暗示扩展源
 * 实际上被豁免了，但文档没有明说 —— 所以这里显式检查并 resume，
 * 并在仍然 suspended 时告警。这是防止「录制正常但用户听不到声音」的廉价保险。
 */
async function getAudioContext() {
  if (!sharedContext) {
    sharedContext = new AudioContext();
    log.info(`AudioContext 已创建，采样率 ${sharedContext.sampleRate}Hz，状态 ${sharedContext.state}`);
  }
  if (sharedContext.state === 'suspended') {
    try {
      await sharedContext.resume();
      log.debug(`AudioContext 已恢复，状态 ${sharedContext.state}`);
    } catch (err) {
      log.warn('AudioContext.resume() 失败，用户可能听不到声音', err);
    }
  }
  return sharedContext;
}

/** 挑一个受支持的 MIME */
function pickMimeType() {
  if (MediaRecorder.isTypeSupported(PREFERRED_MIME)) return PREFERRED_MIME;
  for (const mime of FALLBACK_MIMES) {
    if (mime === '' || MediaRecorder.isTypeSupported(mime)) {
      log.warn(`首选 MIME 不受支持，退化为 "${mime || '(浏览器默认)'}"`);
      return mime;
    }
  }
  return '';
}

/**
 * @typedef {object} RecorderCallbacks
 * @property {(tabId:number, state:import('../common/states.js').TabState, extra?:object) => void} onState
 * @property {(tabId:number, info:{bytes:number, chunks:number, audio:import('../common/audio-level.js').AudioSnapshot|null}) => void} onProgress
 * @property {(tabId:number, result:{blob:Blob, durationMs:number, bytes:number}) => Promise<void>} onFinalize
 * @property {(tabId:number, error:{code:string, message:string}) => void} onError
 */

export class TabRecorder {
  /**
   * @param {object} args
   * @param {number} args.tabId
   * @param {import('../common/protocol.js').TabMeta} args.meta
   * @param {any} args.opts
   * @param {RecorderCallbacks} args.callbacks
   */
  constructor({ tabId, meta, opts, callbacks }) {
    this.tabId = tabId;
    this.meta = meta;
    this.opts = opts;
    this.callbacks = callbacks;

    /** @type {MediaStream | null} */
    this.stream = null;
    /** @type {MediaRecorder | null} */
    this.recorder = null;
    /** @type {AudioContext | null} */
    this.audioContext = null;
    /** @type {MediaStreamAudioSourceNode | null} */
    this.sourceNode = null;
    /** @type {AnalyserNode | null} */
    this.analyser = null;
    /** @type {GainNode | null} */
    this.gainNode = null;
    /** RMS 采样的复用缓冲，避免每秒一次的小分配 */
    /** @type {Float32Array | null} */
    this.levelBuf = null;
    /** 活性状态追踪器（纯逻辑，见 common/audio-level.js） */
    this.tracker = new LevelTracker();

    this.state = /** @type {import('../common/states.js').TabState} */ (S.STARTING);
    this.startedAt = 0;
    this.stoppedAt = 0;
    this.bytes = 0;
    this.seq = 0;
    this.monitor = opts.monitor !== false;
    this.mimeType = '';

    /** @type {ReturnType<typeof setInterval> | null} */
    this.trackPoll = null;
    /** @type {ReturnType<typeof setTimeout> | null} */
    this.stopWatchdog = null;
    /** @type {Promise<void> | null} */
    this.finalizePromise = null;
    /** 已经开始收尾，防止重复触发 */
    this.finalizing = false;
    /** 待写入 IDB 的操作，收尾前必须全部落定 */
    /** @type {Promise<unknown>[]} */
    this.writes = [];
  }

  /**
   * 兑换 streamId 并开始录制。
   *
   * 这个方法 resolve 就意味着「兑换已完成」—— getUserMedia 已返回且
   * MediaRecorder.start() 已调用。SW 的顺序铸造循环等的正是这个 ack，
   * 收到后才会去铸造下一个 streamId。
   *
   * @param {string} streamId
   */
  async start(streamId) {
    // ── 兑换 streamId（单次使用、10 秒 TTL）──
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        // @ts-ignore —— chromeMediaSource 是 Chrome 私有约束，不在标准类型里
        audio: { mandatory: { chromeMediaSource: 'tab', chromeMediaSourceId: streamId } },
        // 音频专用：**完全不写 video 字段**。但必须至少有 audio 或 video，
        // 否则 API 报 "Capture failed. No audio or video requested."
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      const code = /active stream/i.test(message) ? E.TAB_BUSY : E.GUM_FAILED;
      throw Object.assign(new Error(message), { code });
    }

    const tracks = this.stream.getAudioTracks();
    if (tracks.length === 0) {
      this.stream.getTracks().forEach((t) => t.stop());
      throw Object.assign(new Error('捕获的流中没有音轨'), { code: E.GUM_FAILED });
    }

    // ── 分析 + 回放链路：常驻建立，另分一路，绝不参与录制 ──
    await this.attachAudioChain();

    // ── 录制：直接用原始流 ──
    this.mimeType = pickMimeType();
    try {
      this.recorder = new MediaRecorder(this.stream, {
        ...(this.mimeType ? { mimeType: this.mimeType } : {}),
        ...(this.opts.audioBitsPerSecond
          ? { audioBitsPerSecond: this.opts.audioBitsPerSecond }
          : {}),
      });
    } catch (err) {
      this.cleanupStream();
      throw Object.assign(
        new Error(err instanceof Error ? err.message : String(err)),
        { code: E.RECORDER },
      );
    }

    await store.begin({
      sessionId: this.opts.sessionId,
      tabId: this.tabId,
      sessionStamp: this.opts.sessionStamp,
      title: this.meta.title,
      url: this.meta.url,
      index: this.meta.index,
      startedAt: Date.now(),
      mimeType: this.recorder.mimeType || this.mimeType,
    });

    this.wireRecorder();
    this.wireTrackEnded(tracks[0]);

    // timeslice 必须 > 0：它既驱动分块落盘，也把崩溃时的数据丢失上限压到 1 秒。
    // 副作用：这会选中 libwebm 的 live 模式（未知长度 Segment、无 SeekHead/Cues），
    // 那恰好是 EBML 改写最简单的情形。
    this.recorder.start(this.opts.timesliceMs || 1000);

    this.startedAt = Date.now();
    this.state = S.RECORDING;
    log.info(
      `tab=${this.tabId} 录制已开始（${this.recorder.mimeType}，` +
      `timeslice=${this.opts.timesliceMs}ms，监听=${this.monitor}）`,
    );
  }

  /**
   * 建立分析 + 回放链路：source → analyser → gain → destination。
   *
   * 链路**始终建立**（与 monitor 无关）：analyser 供活性指示采样，
   * gain 控制是否回放给用户。monitor 为 false 时 gain 置 0 ——
   * 数据照常流过 analyser（指示可用），但输出到扬声器的是静音。
   */
  async attachAudioChain() {
    if (!this.stream || this.sourceNode) return;
    try {
      const ac = await getAudioContext();
      this.audioContext = ac;
      this.sourceNode = ac.createMediaStreamSource(this.stream);
      this.analyser = ac.createAnalyser();
      this.analyser.fftSize = 2048; // 48kHz 下约 42ms 窗口，足够判断语音有无
      this.gainNode = ac.createGain();
      this.gainNode.gain.value = this.monitor ? 1 : 0;
      this.sourceNode.connect(this.analyser);
      this.analyser.connect(this.gainNode);
      this.gainNode.connect(ac.destination);
    } catch (err) {
      // 链路失败不该让录制失败 —— 录音是主要目的，指示与回放是便利功能
      log.warn(`tab=${this.tabId} 建立分析/回放链路失败，录制继续（活性指示将不可用）`, err);
    }
  }

  /**
   * 采样一次电平并更新活性追踪器。
   *
   * AudioContext 被自动播放策略卡住（suspended）时 analyser 读到的全是 0，
   * 若照常被记成「静音」会产生吓用户的假警告 —— 此时返回 null，
   * 面板端不显示指示，而不是显示错误状态。
   *
   * @returns {import('../common/audio-level.js').AudioSnapshot | null}
   */
  sampleAudio() {
    if (!this.analyser || !this.audioContext || this.audioContext.state !== 'running') {
      return null;
    }
    if (!this.levelBuf || this.levelBuf.length !== this.analyser.fftSize) {
      this.levelBuf = new Float32Array(this.analyser.fftSize);
    }
    this.analyser.getFloatTimeDomainData(this.levelBuf);
    this.tracker.update(computeRms(this.levelBuf));
    return this.tracker.snapshot();
  }

  /** @param {boolean} enabled */
  async setMonitor(enabled) {
    this.monitor = enabled;
    if (enabled) {
      if (!this.sourceNode) await this.attachAudioChain();
      if (this.gainNode) this.gainNode.gain.value = 1;
    } else if (this.gainNode) {
      // 用 gain 归零而不是断开节点 —— 重新连接会有爆音，改增益是平滑的
      this.gainNode.gain.value = 0;
    }
    log.debug(`tab=${this.tabId} 监听 ${enabled ? '开' : '关'}`);
  }

  wireRecorder() {
    const rec = /** @type {MediaRecorder} */ (this.recorder);

    rec.ondataavailable = (event) => {
      if (!event.data || event.data.size === 0) return;
      const seq = this.seq;
      this.seq += 1;
      this.bytes += event.data.size;

      // 立刻写 IDB。把 promise 记下来，收尾时必须等它们全部落定，
      // 否则可能在最后一块还没落盘时就去读取拼接。
      const write = store
        .append(this.opts.sessionId, this.tabId, seq, event.data)
        .then((info) => {
          // 分块到达的节奏（≈每秒一次）正好是活性采样的节奏，
          // 不需要额外的定时器
          this.callbacks.onProgress(this.tabId, { ...info, audio: this.sampleAudio() });
        })
        .catch((err) => {
          log.error(`tab=${this.tabId} 分块 ${seq} 写入失败`, err);
          this.callbacks.onError(this.tabId, {
            code: E.STORE, message: '录音数据写入失败',
          });
        });
      this.writes.push(write);
    };

    rec.onerror = (event) => {
      const err = /** @type {any} */ (event).error;
      log.error(`tab=${this.tabId} MediaRecorder 错误`, err);
      this.callbacks.onError(this.tabId, {
        code: E.RECORDER, message: err?.message ?? '录制器内部错误',
      });
      void this.finalize('recorder_error');
    };

    rec.onstop = () => {
      log.debug(`tab=${this.tabId} onstop 触发`);
      if (this.stopWatchdog !== null) {
        clearTimeout(this.stopWatchdog);
        this.stopWatchdog = null;
      }
      void this.finalize('onstop');
    };
  }

  /**
   * 尾块保障主路径：track 的 ended。
   *
   * Tab 关闭、捕获被系统杀掉都会触发这个。此时必须**先 requestData() 再 stop()**：
   * requestData 会把当前缓冲区的数据冲出来变成一个 dataavailable。
   * （stop() 单独是否也能冲出缓冲块，我不确定 —— requestData 属于冗余保险，无害。）
   *
   * @param {MediaStreamTrack} track
   */
  wireTrackEnded(track) {
    track.addEventListener('ended', () => {
      log.info(`tab=${this.tabId} 音轨结束（标签页可能已关闭）`);
      void this.stop('track_ended');
    });

    // 第三重保险：轮询 readyState，兜住 ended 事件完全没触发的情况
    this.trackPoll = setInterval(() => {
      if (track.readyState === 'ended') {
        log.warn(`tab=${this.tabId} 轮询发现音轨已结束（ended 事件未触发）`);
        void this.stop('track_poll');
      }
    }, TRACK_POLL_MS);
  }

  /**
   * 停止录制。可重复调用（幂等）。
   * @param {string} reason
   */
  async stop(reason = 'user') {
    if (this.state === S.STOPPING || this.finalizing || this.state === S.DONE) {
      log.debug(`tab=${this.tabId} 已在停止流程中，忽略重复请求（${reason}）`);
      return;
    }

    this.state = S.STOPPING;
    this.stoppedAt = Date.now();
    this.callbacks.onState(this.tabId, S.STOPPING);
    log.info(`tab=${this.tabId} 停止中（原因 ${reason}）`);

    this.clearTrackPoll();

    const rec = this.recorder;
    if (!rec || rec.state === 'inactive') {
      // recorder 已经不活跃了，直接进收尾
      await this.finalize(reason);
      return;
    }

    // 看门狗：onstop 若 5 秒不来，就用已收到的分块抢救，而不是永久挂起
    this.stopWatchdog = setTimeout(() => {
      log.warn(`tab=${this.tabId} onstop 超时，用已有分块抢救`);
      this.stopWatchdog = null;
      void this.finalize('watchdog');
    }, STOP_WATCHDOG_MS);

    try {
      // 先把缓冲区冲出来，再停 —— 保住最后一块
      if (rec.state === 'recording') {
        rec.requestData();
      }
      rec.stop();
    } catch (err) {
      log.warn(`tab=${this.tabId} 调用 stop() 出错`, err);
      void this.finalize('stop_error');
    }
  }

  /**
   * 收尾：等所有写入落定 → 从 IDB 读回分块 → 交给上层拼接改写并保存。
   * @param {string} reason
   */
  async finalize(reason) {
    if (this.finalizing) return this.finalizePromise ?? undefined;
    this.finalizing = true;

    this.finalizePromise = (async () => {
      this.state = S.FINALIZING;
      this.callbacks.onState(this.tabId, S.FINALIZING);
      this.clearTrackPoll();
      this.cleanupStream();

      try {
        // 关键：等所有 IDB 写入落定，否则可能漏掉最后一块
        await Promise.allSettled(this.writes);

        await store.setStatus(this.opts.sessionId, this.tabId, 'complete');
        const chunks = await store.readChunks(this.opts.sessionId, this.tabId);

        if (chunks.length === 0) {
          log.error(`tab=${this.tabId} 没有任何分块（原因 ${reason}）`);
          this.callbacks.onError(this.tabId, {
            code: E.NO_DATA, message: '没有录到任何音频数据',
          });
          return;
        }

        // 惰性拼接：Blob 构造器按引用持有分片，不实体化字节
        const mime = this.recorder?.mimeType || this.mimeType || 'audio/webm';
        const merged = new Blob(chunks, { type: mime });
        const durationMs = (this.stoppedAt || Date.now()) - this.startedAt;

        log.info(
          `tab=${this.tabId} 收尾完成：${chunks.length} 块，` +
          `${merged.size} 字节，时长 ${Math.round(durationMs / 1000)}s`,
        );

        await this.callbacks.onFinalize(this.tabId, {
          blob: merged, durationMs, bytes: merged.size,
        });
      } catch (err) {
        log.error(`tab=${this.tabId} 收尾失败`, err);
        this.callbacks.onError(this.tabId, {
          code: E.UNKNOWN,
          message: err instanceof Error ? err.message : String(err),
        });
      }
    })();

    return this.finalizePromise;
  }

  clearTrackPoll() {
    if (this.trackPoll !== null) {
      clearInterval(this.trackPoll);
      this.trackPoll = null;
    }
  }

  /** 断开音频节点并停掉所有轨道，释放捕获 */
  cleanupStream() {
    try {
      this.gainNode?.disconnect();
      this.analyser?.disconnect();
      this.sourceNode?.disconnect();
    } catch { /* 已断开 */ }
    this.gainNode = null;
    this.analyser = null;
    this.sourceNode = null;
    this.audioContext = null; // 共享 context 本身保留，只解除引用
    this.levelBuf = null;

    if (this.stream) {
      for (const track of this.stream.getTracks()) {
        try { track.stop(); } catch { /* 已停止 */ }
      }
      this.stream = null;
    }
  }

  /** 供 PING 对账用的状态快照 */
  snapshot() {
    return {
      tabId: this.tabId,
      state: this.state,
      startedAt: this.startedAt,
      bytes: this.bytes,
      chunks: this.seq,
      monitor: this.monitor,
      audio: this.tracker.snapshot(),
    };
  }
}
