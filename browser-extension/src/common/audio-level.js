// @ts-check
/**
 * 音频活性检测 —— 纯逻辑，无 DOM、无 chrome.*，可被 node:test 直接 import。
 *
 * ## 为什么需要这个模块
 *
 * 本扩展默认**静默录制**（用户听不到标签页的声音），这带来一个盲区：
 * 用户无法确认「是不是真的录到了声音」。最典型的事故是标签页已暂停，
 * 录了两小时才发现文件全程静音。
 *
 * 解法是在 offscreen 的音频链路上挂一个 AnalyserNode（只读分析，
 * **绝不进入编码链路**），每秒算一次 RMS，随进度心跳上报给面板显示
 * 「有声 / 静音」指示，并在录制开始一段时间后仍完全无声时给出警告。
 *
 * 本模块只包含可测试的纯逻辑：RMS 计算与活性状态追踪。
 * AnalyserNode 的接线在 offscreen/recorder.js。
 */

/**
 * 静音判定的 RMS 阈值，约 -60 dBFS。
 *
 * 数字静音的 RMS 是 0；1% 音量的语音 RMS 约 0.01，远高于阈值。
 * 取 -60dB 而不是更高的 -40dB，是为了宁可把极小声误判为「有声」，
 * 也绝不把真实播放误判为静音 —— 后者的代价是吓用户的假警告。
 */
export const SILENCE_RMS_THRESHOLD = 0.001;

/** 录制开始后持续无声超过这个时长，面板显示「未检测到声音」警告 */
export const NO_SOUND_WARN_MS = 10_000;

/** 连续静音低于这个时长时显示「有声」，否则显示「静音」 */
export const SILENT_INDICATOR_MS = 3_000;

/**
 * 计算一段时域采样的 RMS（均方根），取值 0..1。
 * @param {Float32Array | number[]} samples
 * @returns {number}
 */
export function computeRms(samples) {
  if (!samples || samples.length === 0) return 0;
  let sum = 0;
  for (let i = 0; i < samples.length; i += 1) {
    sum += samples[i] * samples[i];
  }
  return Math.sqrt(sum / samples.length);
}

/**
 * @typedef {object} AudioSnapshot
 * @property {number} level 最近一次采样的 RMS（0..1）
 * @property {boolean} hasSound 录制以来是否检测到过高於阈值的信号
 * @property {number} silentMs 连续静音时长；从未有声时自首次采样算起
 */

/**
 * 单个录制的活性状态追踪器。
 *
 * 每次采样调一次 update()，之后用 snapshot() 取可 JSON 序列化的快照
 * （会随消息传到 SW、再经 storage.session 镜像到 panel，所以**必须**
 * 全是有限数值 —— 不能用 Infinity，JSON 会把它变成 null）。
 */
export class LevelTracker {
  /**
   * @param {object} [opts]
   * @param {number} [opts.threshold] 有声判定的 RMS 阈值
   * @param {() => number} [opts.now] 时钟，测试时可注入假时钟
   */
  constructor({ threshold = SILENCE_RMS_THRESHOLD, now = () => Date.now() } = {}) {
    this.threshold = threshold;
    this.now = now;
    /** @type {number} */
    this.level = 0;
    /** @type {boolean} */
    this.hasSound = false;
    /** 首次采样时间；silentMs 在从未有声时以它为基准 */
    this.startedAt = 0;
    /** 最近一次有声的时间 */
    this.lastSoundAt = 0;
  }

  /**
   * 喂入一次 RMS 采样。
   * @param {number} rms
   */
  update(rms) {
    const t = this.now();
    if (this.startedAt === 0) this.startedAt = t;
    this.level = rms;
    if (rms >= this.threshold) {
      this.hasSound = true;
      this.lastSoundAt = t;
    }
  }

  /**
   * 连续静音时长（毫秒）。
   * 从未检测到声音时，自首次采样算起 —— 这让「开始录制后一直静音」
   * 与「有声之后转入静音」共用同一个阈值判断，UI 不需要知道区别。
   * @returns {number}
   */
  get silentMs() {
    const base = this.lastSoundAt || this.startedAt;
    if (base === 0) return 0; // 还没有任何采样
    return Math.max(0, this.now() - base);
  }

  /** @returns {AudioSnapshot} */
  snapshot() {
    return {
      level: this.level,
      hasSound: this.hasSound,
      silentMs: this.silentMs,
    };
  }
}
