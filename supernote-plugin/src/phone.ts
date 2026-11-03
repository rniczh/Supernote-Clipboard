import {useSyncExternalStore} from 'react';
import {NativeEventEmitter, NativeModules} from 'react-native';
import RNFS from 'react-native-fs';
import {PluginManager} from 'sn-plugin-lib';
import {ensureInternet} from './connection';
import {PERM_FILE_WRITE, SAVE_FOLDER} from './constants';
import {localAddresses} from './discovery';
import {toUserError, UserError} from './errors';
import {insertImage, insertTextBox, maxImageWidth} from './paste';
import {ensurePermission} from './permissions';
import {phonePage} from './phonePage';
import {toast} from './toast';
import {openScreen} from './uiState';

/** The phone link stops working after this long without a scan or upload. */
export const PHONE_IDLE_MINUTES = 10;
const MAX_TEXT_CHARS = 20000;

type PhoneEvent = {kind: 'connected' | 'image' | 'text'; path?: string; text?: string};

export type PhoneSession = {
  status: 'off' | 'starting' | 'waiting' | 'connected' | 'error';
  url: string | null;
  message: string | null;
};

const server = NativeModules.PCPhoneServer;
let state: PhoneSession = {status: 'off', url: null, message: null};
const listeners = new Set<() => void>();
let idleTimer: ReturnType<typeof setTimeout> | null = null;
let queue: Promise<void> = Promise.resolve();

function update(next: Partial<PhoneSession>) {
  state = {...state, ...next};
  listeners.forEach(listener => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function usePhoneSession(): PhoneSession {
  return useSyncExternalStore(subscribe, () => state);
}

function touch() {
  if (idleTimer) {
    clearTimeout(idleTimer);
  }
  idleTimer = setTimeout(() => {
    stopPhoneSession('The phone link expired. Tap "New code" to send more.');
  }, PHONE_IDLE_MINUTES * 60 * 1000);
}

export async function startPhoneSession() {
  if (!server?.start) {
    update({status: 'error', url: null, message: 'This plugin build has no phone support.'});
    return;
  }
  update({status: 'starting', url: null, message: null});
  try {
    await ensureInternet();
    await ensurePermission(
      PERM_FILE_WRITE,
      `PC Clipboard saves photos from your phone to the ${SAVE_FOLDER} folder before inserting them.`,
    );
    const wifi = (await localAddresses()).find(a => !a.address.startsWith('169.254.'));
    if (!wifi) {
      throw new UserError('This Supernote is not on Wi-Fi. Connect it to the same Wi-Fi as your phone.');
    }
    const page = phonePage(await maxImageWidth(), MAX_TEXT_CHARS);
    const dir = `${RNFS.ExternalStorageDirectoryPath}/${SAVE_FOLDER}`;
    const {port, token}: {port: number; token: string} = await server.start(page, dir);
    update({status: 'waiting', url: `http://${wifi.address}:${port}/${token}`, message: null});
    touch();
  } catch (e) {
    update({status: 'error', url: null, message: toUserError(e).message});
  }
}

export function stopPhoneSession(message: string | null = null) {
  if (idleTimer) {
    clearTimeout(idleTimer);
    idleTimer = null;
  }
  server?.stop?.().catch(() => {});
  update({status: 'off', url: null, message});
}

async function insertFromPhone(event: PhoneEvent) {
  try {
    await PluginManager.closePluginView().catch(() => {});
    if (event.kind === 'image' && event.path) {
      await insertImage(event.path);
      toast('Photo pasted.');
    } else if (event.kind === 'text' && event.text) {
      const fits = await insertTextBox(event.text.slice(0, MAX_TEXT_CHARS));
      toast(fits ? 'Text pasted.' : 'Text pasted. It is long, so part of it may be off the page.');
    }
  } catch (e) {
    openScreen('phone', {text: toUserError(e).message, error: true});
    await PluginManager.showPluginView().catch(() => {});
  }
}

if (server) {
  new NativeEventEmitter(server).addListener('PCPhoneEvent', (event: PhoneEvent) => {
    if (state.status === 'off') {
      return;
    }
    touch();
    if (event.kind === 'connected') {
      update({status: 'connected'});
      return;
    }
    // One at a time, so several photos from one pick are inserted in order.
    queue = queue.then(() => insertFromPhone(event));
  });
}
