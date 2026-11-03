import RNFS from 'react-native-fs';
import {PluginCommAPI, PluginFileAPI, PluginManager, PluginNoteAPI} from 'sn-plugin-lib';
import {
  getClipText,
  getLatestClip,
  shotUrl,
  tokenHeaders,
  unreachableError,
  type Clip,
} from './bridge';
import {connect, type Progress} from './connection';
import {PERM_FILE_READ, PERM_FILE_WRITE, SAVE_FOLDER} from './constants';
import {toUserError, UserError} from './errors';
import {httpDownload} from './http';
import {ensurePermission} from './permissions';
import {asResult} from './sdk';
import {type Settings} from './settings';
import {toast} from './toast';
import {openScreen} from './uiState';

const ERROR_NO_READ_PERMISSION = 1503;
/** Inserted screenshots and text boxes take at most this share of the page width. */
const PAGE_WIDTH_SHARE = 0.85;
/** Text box font size in page pixels; both models are 300 PPI, so this is about 10 pt. */
const TEXT_FONT_SIZE = 42;
const TEXT_LINE_HEIGHT = 1.4;
/** Characters drawn about as wide as they are tall (CJK, Hangul, full-width forms). */
const WIDE_CHAR_RE = /[\u1100-\u115f\u2e80-\ua4cf\uac00-\ud7a3\uf900-\ufaff\ufe30-\ufe4f\uff00-\uff60\uffe0-\uffe6]/;

type Size = {width: number; height: number};

let busy = false;

/** Page size in pixels of the open note, falling back to the device's screen size. */
async function currentPageSize(): Promise<Size> {
  try {
    const path = asResult<string>(await PluginCommAPI.getCurrentFilePath());
    const page = asResult<number>(await PluginCommAPI.getCurrentPageNum());
    if (path.success && path.result && page.success && typeof page.result === 'number') {
      const size = asResult<Size>(await PluginFileAPI.getPageSize(path.result, page.result));
      if (size.success && size.result?.width && size.result?.height) {
        return size.result;
      }
    }
  } catch {
    // Fall through to the device default.
  }
  try {
    // 5 = A5 X2 (Manta), 1920 x 2560; the other plugin-capable models are 1404 x 1872.
    if ((await PluginManager.getDeviceType()) === 5) {
      return {width: 1920, height: 2560};
    }
  } catch {
    // Use the smaller page.
  }
  return {width: 1404, height: 1872};
}

/** Widest image worth sending for the open page, in pixels. */
export async function maxImageWidth() {
  return Math.round((await currentPageSize()).width * PAGE_WIDTH_SHARE);
}

async function download(settings: Settings, clip: Clip): Promise<string> {
  await ensurePermission(
    PERM_FILE_WRITE,
    `PC Clipboard saves screenshots to the ${SAVE_FOLDER} folder before inserting them.`,
  );
  const maxWidth = await maxImageWidth();
  const dir = `${RNFS.ExternalStorageDirectoryPath}/${SAVE_FOLDER}`;
  const file = `${dir}/${clip.id}_w${maxWidth}_g100.${clip.ext}`;
  if (await RNFS.exists(file)) {
    return file;
  }
  await RNFS.mkdir(dir);

  const partial = `${file}.part`;
  let status = 0;
  try {
    status = (
      await httpDownload(shotUrl(settings, clip, maxWidth), tokenHeaders(settings), partial, 30000)
    ).status;
  } catch (e) {
    throw unreachableError(settings, e);
  }
  if (status !== 200) {
    throw new UserError(
      status === 401
        ? 'This Supernote is no longer paired. Open Settings and tap "Find my computer".'
        : `Download failed (HTTP ${status}). The screenshot may have been deleted on your computer.`,
      status === 401 ? 'config' : 'network',
    );
  }
  await RNFS.moveFile(partial, file);
  return file;
}

export async function insertImage(file: string) {
  let res = asResult<boolean>(await PluginNoteAPI.insertImage(file));
  if (!res.success && res.error?.code === ERROR_NO_READ_PERMISSION) {
    await ensurePermission(
      PERM_FILE_READ,
      `PC Clipboard reads the downloaded screenshot from the ${SAVE_FOLDER} folder.`,
    );
    res = asResult<boolean>(await PluginNoteAPI.insertImage(file));
  }
  if (!res.success || res.result === false) {
    throw new UserError(
      `The note did not accept the image${res.error?.message ? `: ${res.error.message}` : '.'}`,
    );
  }
}

/** Rough number of wrapped lines, so the text box is about as tall as its text. */
function estimateLines(text: string, boxWidth: number, fontSize: number) {
  let lines = 0;
  for (const paragraph of text.split('\n')) {
    let width = 0;
    for (const char of paragraph) {
      width += WIDE_CHAR_RE.test(char) ? fontSize : fontSize * 0.55;
    }
    lines += Math.max(1, Math.ceil(width / boxWidth));
  }
  return lines;
}

/** Insert text as an editable text box, centred on the page. Returns false when it may not fit. */
export async function insertTextBox(text: string): Promise<boolean> {
  const page = await currentPageSize();
  const margin = Math.round((page.width * (1 - PAGE_WIDTH_SHARE)) / 2);
  const boxWidth = page.width - 2 * margin;
  const needed = Math.ceil(
    (estimateLines(text, boxWidth, TEXT_FONT_SIZE) + 1) * TEXT_FONT_SIZE * TEXT_LINE_HEIGHT,
  );
  const height = Math.min(needed, page.height - 2 * margin);
  const top = Math.max(margin, Math.round((page.height - height) / 2));

  const res = asResult<boolean>(
    await PluginNoteAPI.insertText({
      textContentFull: text,
      fontSize: TEXT_FONT_SIZE,
      textRect: {left: margin, top, right: margin + boxWidth, bottom: top + height},
      textAlign: 0,
      textBold: 0,
      textItalics: 0,
      textFrameWidthType: 0,
      textEditable: 0,
    }),
  );
  if (!res.success || res.result === false) {
    throw new UserError(
      `The note did not accept the text${res.error?.message ? `: ${res.error.message}` : '.'}`,
    );
  }
  return needed <= height;
}

/** Paste a clip from the computer (the newest one by default) into the open note. */
export async function pasteClip(clip?: Clip, progress: Progress = toast) {
  if (busy) {
    throw new UserError('Already pasting.');
  }
  busy = true;
  try {
    const settings = await connect({progress});
    const target = clip ?? (await getLatestClip(settings));
    if (!target) {
      throw new UserError(
        'Nothing to paste yet. Take a screenshot or copy some text on your computer first.',
      );
    }
    if (target.kind === 'text') {
      const fits = await insertTextBox(await getClipText(settings, target));
      return fits ? 'Text pasted.' : 'Text pasted. It is long, so part of it may be off the page.';
    }
    await insertImage(await download(settings, target));
    return 'Screenshot pasted.';
  } finally {
    busy = false;
  }
}

/** Paste and report the outcome; on failure the plugin view opens with the reason. */
export async function pasteAndReport(clip?: Clip) {
  try {
    toast('Pasting...');
    toast(await pasteClip(clip));
  } catch (e) {
    const error = toUserError(e);
    openScreen(error.kind === 'config' ? 'settings' : 'shots', {
      text: error.message,
      error: true,
    });
    await PluginManager.showPluginView().catch(() => {});
  }
}
