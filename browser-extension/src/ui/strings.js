// @ts-check
/**
 * 全部界面文案，集中一处。
 *
 * 刻意**不用** _locales / __MSG_ 机制：单语言下那套替换是纯仪式，
 * 却要付出「文案散落在 JSON 里、无法 grep、改一处要跳三个文件」的代价。
 * 集中在这里意味着日后要做多语言时，抽取工作是机械的、不碰任何逻辑。
 */

export const T = {
  sendToTingji: '在听记中处理',
  tingjiRetry: '重试送入听记',
  tingjiSending: '正在送入听记…',
  tingjiQueued: '已加入听记队列',
  tingjiReused: '已有相同录音，直接复用',
  openTingji: '打开听记',
  connectorReady: '听记连接器已安装 · 仅连接本机',

  appName: '听记 · 标签页录音',

  // ── 空状态 ──
  emptyTitle: '还没有可录制的标签页',
  emptyHint: '播放一些音频，或切到目标标签页按快捷键授权。',

  // ── 说明条 ──
  explainerTitle: '为什么需要逐个授权？',
  explainerBody:
    'Chrome 要求扩展必须在某个标签页上被触发过，才允许录制该标签页。' +
    '切到目标标签页，按下快捷键（或点击扩展图标 / 用右键菜单）即可授权。' +
    '已授权的标签页，扩展图标上会显示 ● 徽标。',
  explainerDismiss: '知道了，不再提示',

  // ── 状态标签 ──
  stateIdle: '未授权',
  stateArmed: '已就绪',
  stateStarting: '启动中',
  stateRecording: '录制中',
  stateStopping: '停止中',
  stateFinalizing: '处理中',
  stateSaving: '保存中',
  stateDone: '已完成',
  stateError: '出错',

  // ── 操作按钮 ──
  actionStartAll: '开始录制',
  actionStopAll: '全部停止',
  actionStart: '开始录制',
  actionStop: '停止',
  actionRemove: '移除',
  actionRetry: '重试',
  actionRecordAgain: '再次录制',
  actionGoToTab: '前往该标签页',
  actionMonitorOn: '静音回放',
  actionMonitorOff: '恢复回放',

  // ── 全局回放开关 ──
  monitorGlobalLabel: '录制时播放声音',
  /**
   * 这句话承担一个具体职责：默认静音后，用户开录会发现标签页没声了，
   * 很容易以为扩展坏了。必须明确告诉他们这是刻意的、且不影响文件。
   */
  monitorGlobalHintMuted: '当前为静默录制：你听不到声音，但录音文件完整。',
  monitorGlobalHintOn: '录制时可正常听到声音。',

  // ── 音频活性指示 ──
  audioActive: '● 有声',
  audioSilent: '○ 静音',
  /**
   * 长时间完全无声时的行内警告。这针对一个真实事故模式：
   * 标签页已暂停（或播放器卡死），用户以为在录，其实文件全程静音。
   */
  audioNoSound: '未检测到声音 —— 确认标签页正在播放？',

  // ── 提示 ──
  hintArmThisTab: (/** @type {string} */ key) => `切到该标签页按 ${key} 授权`,
  hintNotCapturable: '该页面无法录制',
  monitorTitle: '控制录制时你是否还能听到这个标签页的声音（不影响录音文件）',
  // ── 恢复 ──
  recoverTitle: (/** @type {number} */ n) => `发现 ${n} 段未保存的录音`,
  recoverBody: '扩展可能在上次录制期间被重载或崩溃。这些录音仍可保存。',
  recoverSave: '保存',
  recoverDiscard: '丢弃',
  recoverSaveAll: '全部保存',
  recoverDiscardAll: '全部丢弃',

  // ── 汇总 ──
  summaryArmed: (/** @type {number} */ n) => `${n} 个标签页已就绪`,
  summaryRecording: (/** @type {number} */ n) => `正在录制 ${n} 个标签页`,
  summaryNone: '尚未授权任何标签页',

  // ── 提示与警告 ──
  warnManyTabs: '同时录制过多标签页可能导致卡顿，建议不超过 8 个。',
  savedAs: (/** @type {string} */ name) => `已保存：${name}`,
  toastArmed: '已加入录制列表',
  toastStopped: '已停止',
  errPrefix: '错误：',
};

/** 快捷键的显示文本。macOS 用符号，其他平台用单词。 */
export function shortcutLabel() {
  const isMac = navigator.userAgent.includes('Mac');
  return isMac ? '⌥⇧R' : 'Alt+Shift+R';
}

/**
 * 状态 → 中文标签。
 * @param {string} state
 */
export function stateLabel(state) {
  switch (state) {
    case 'idle': return T.stateIdle;
    case 'armed': return T.stateArmed;
    case 'starting': return T.stateStarting;
    case 'recording': return T.stateRecording;
    case 'stopping': return T.stateStopping;
    case 'finalizing': return T.stateFinalizing;
    case 'saving': return T.stateSaving;
    case 'done': return T.stateDone;
    case 'error': return T.stateError;
    default: return state;
  }
}
