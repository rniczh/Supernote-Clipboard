import RNFS from 'react-native-fs';
import {PluginCommAPI, PluginFileAPI, PluginManager, PluginNoteAPI} from 'sn-plugin-lib';
import {getLatestShot, shotUrl, tokenHeaders, unreachableError, type Shot} from './bridge';
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
/** Inserted screenshots take at most this share of the page width. */
const PAGE_WIDTH_SHARE = 0.85;

let busy = false;

/** Page width in pixels of the open note, falling back to the device's screen width. */
async function currentPageWidth(): Promise<number> {
  try {
    const path = asResult<string>(await PluginCommAPI.getCurrentFilePath());
    const page = asResult<number>(await PluginCommAPI.getCurrentPageNum());
    if (path.success && path.result && page.success && typeof page.result === 'number') {
      const size = asResult<{width: number; height: number}>(
        await PluginFileAPI.getPageSize(path.result, page.result),
      );
      if (size.success && size.result?.width) {
        return size.result.width;
      }
    }
  } catch {
    // Fall through to the device default.
  }
  try {
    // 5 = A5 X2 (Manta), 1920 px wide; the other plugin-capable models are 1404 px wide.
    return (await PluginManager.getDeviceType()) === 5 ? 1920 : 1404;
  } catch {
    return 1404;
  }
}

async function download(settings: Settings, shot: Shot): Promise<string> {
  await ensurePermission(
    PERM_FILE_WRITE,
    `PC Clipboard saves screenshots to the ${SAVE_FOLDER} folder before inserting them.`,
  );
  const maxWidth = Math.round((await currentPageWidth()) * PAGE_WIDTH_SHARE);
  const dir = `${RNFS.ExternalStorageDirectoryPath}/${SAVE_FOLDER}`;
  const file = `${dir}/${shot.id}_w${maxWidth}.${shot.ext}`;
  if (await RNFS.exists(file)) {
    return file;
  }
  await RNFS.mkdir(dir);

  const partial = `${file}.part`;
  let status = 0;
  try {
    status = (
      await httpDownload(shotUrl(settings, shot, maxWidth), tokenHeaders(settings), partial, 30000)
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

async function insertImage(file: string) {
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

/** Download a screenshot from the computer (the newest one by default) and insert it into the open note. */
export async function pasteShot(shot?: Shot, progress: Progress = toast) {
  if (busy) {
    throw new UserError('A screenshot is already being pasted.');
  }
  busy = true;
  try {
    const settings = await connect({progress});
    const target = shot ?? (await getLatestShot(settings));
    if (!target) {
      throw new UserError(
        'There are no screenshots on your computer yet. Take one with Supernote Clipboard first.',
      );
    }
    const file = await download(settings, target);
    await insertImage(file);
  } finally {
    busy = false;
  }
}

/** Paste and report the outcome; on failure the plugin view opens with the reason. */
export async function pasteAndReport(shot?: Shot) {
  try {
    toast('Pasting screenshot...');
    await pasteShot(shot);
    toast('Screenshot pasted.');
  } catch (e) {
    const error = toUserError(e);
    openScreen(error.kind === 'config' ? 'settings' : 'shots', {
      text: error.message,
      error: true,
    });
    await PluginManager.showPluginView().catch(() => {});
  }
}
