import RNFS from 'react-native-fs';
import {PluginManager} from 'sn-plugin-lib';
import {DEFAULT_PORT} from './constants';

/** The computer this Supernote is paired with. All fields are empty until pairing succeeds. */
export type Settings = {
  /** Last known base URL, e.g. "http://192.168.1.20:8765". Updated when the computer's IP changes. */
  serverUrl: string;
  serverId: string;
  serverName: string;
  /** Issued by the computer when the pairing request is allowed. */
  token: string;
};

export const EMPTY_SETTINGS: Settings = {
  serverUrl: '',
  serverId: '',
  serverName: '',
  token: '',
};
let cached: Settings | null = null;

/** The plugin's private directory needs no permission, unlike shared storage. */
async function settingsFile(): Promise<string> {
  let dir: string | null | undefined;
  try {
    dir = await PluginManager.getPluginDirPath();
  } catch {
    dir = null;
  }
  return `${dir || RNFS.DocumentDirectoryPath}/pc-clipboard-settings.json`;
}

function str(value: unknown) {
  return typeof value === 'string' ? value : '';
}

export async function loadSettings(): Promise<Settings> {
  if (cached) {
    return cached;
  }
  try {
    const file = await settingsFile();
    if (await RNFS.exists(file)) {
      const data = JSON.parse(await RNFS.readFile(file, 'utf8'));
      cached = {
        serverUrl: str(data.serverUrl),
        serverId: str(data.serverId),
        serverName: str(data.serverName),
        token: str(data.token),
      };
      return cached;
    }
  } catch {
    // Unreadable settings are treated as "not paired yet".
  }
  return EMPTY_SETTINGS;
}

export async function saveSettings(settings: Settings) {
  cached = settings;
  await RNFS.writeFile(await settingsFile(), JSON.stringify(settings), 'utf8');
}

/**
 * Accepts what people type ("192.168.1.20", "192.168.1.20:8765", "http://my-pc.local:8765/")
 * and returns "http://host:port", or "" when the input is not a usable address.
 */
export function normalizeServerUrl(input: string): string {
  const match = input
    .trim()
    .match(/^(?:(https?):\/\/)?([a-z0-9.-]+|\[[0-9a-f:]+\])(?::(\d{1,5}))?\/*$/i);
  if (!match) {
    return '';
  }
  const [, scheme = 'http', host, port = String(DEFAULT_PORT)] = match;
  return `${scheme.toLowerCase()}://${host}:${port}`;
}

/** "http://192.168.1.20:8765" -> "192.168.1.20:8765" for display. */
export function displayAddress(serverUrl: string): string {
  return serverUrl.replace(/^http:\/\//i, '');
}
