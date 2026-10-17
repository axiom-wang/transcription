// @ts-check
export const HOST = 'local.audio_notes.tingji';
export const INSTALL_HINT = '未安装听记连接器，请运行 scripts/install_browser_extension.py';

/** @param {any} message */
export async function sendToTingji(message) {
  try {
    const result = await chrome.runtime.sendNativeMessage(HOST, message);
    if (!result || typeof result.ok !== 'boolean') {
      return { ok: false, error: '听记连接器回复无效，请重新安装连接器' };
    }
    return result;
  } catch (err) {
    const raw = err instanceof Error ? err.message : String(err);
    if (/native messaging host not found/i.test(raw)) {
      return { ok: false, error: INSTALL_HINT };
    }
    return { ok: false, error: '无法连接听记，请检查连接器安装后重试' };
  }
}

/** @param {number} downloadId */
export async function importDownload(downloadId) {
  try {
    const [download] = await chrome.downloads.search({ id: downloadId });
    if (!download || download.exists === false) {
      return { ok: false, error: '找不到录音文件，可能已被移动或删除，请重新保存' };
    }
    if (download.state !== 'complete') {
      return { ok: false, error: '录音尚未下载完成，请等待保存完成后重试' };
    }
    if (!download.filename || !download.filename.startsWith('/')) {
      return { ok: false, error: '无法取得录音的本机路径，请重新保存' };
    }
    return await sendToTingji({ type: 'import', path: download.filename });
  } catch {
    return { ok: false, error: '无法读取下载记录，请重新保存录音后重试' };
  }
}
