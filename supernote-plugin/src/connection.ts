import {PluginManager} from 'sn-plugin-lib';
import {
  describeNetworkError,
  hello,
  pairingStatus,
  probe,
  requestPairing,
  type ServerInfo,
} from './bridge';
import {DEFAULT_PORT, PERM_INTERNET} from './constants';
import {findComputer, type SearchReport} from './discovery';
import {UserError} from './errors';
import {ensurePermission} from './permissions';
import {displayAddress, loadSettings, saveSettings, type Settings} from './settings';

export type Progress = (message: string) => void;

const PAIRING_TIMEOUT_MS = 110_000;
const PAIRING_POLL_MS = 1500;
const PAIRING_REMINDER_MS = 4000;

const DEVICE_NAMES: Record<number, string> = {
  0: 'Supernote A5',
  1: 'Supernote A6',
  2: 'Supernote A6 X',
  3: 'Supernote A5 X',
  4: 'Supernote Nomad',
  5: 'Supernote Manta',
};

export async function ensureInternet() {
  await ensurePermission(
    PERM_INTERNET,
    'PC Clipboard needs network access to download screenshots from your computer.',
  );
}

function sleep(ms: number) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function deviceName() {
  try {
    return DEVICE_NAMES[await PluginManager.getDeviceType()] ?? 'Supernote';
  } catch {
    return 'Supernote';
  }
}

function parseUrl(serverUrl: string) {
  const match = serverUrl.match(/^https?:\/\/([^/:]+):(\d+)/);
  return match ? {host: match[1], port: Number(match[2])} : {host: '', port: DEFAULT_PORT};
}

function notFoundError({ownAddresses, subnets, unusualError}: SearchReport) {
  const searched = subnets.map(s => `${s}.x`).join(', ');
  const self = ownAddresses.length
    ? `this Supernote is ${ownAddresses.join(', ')}`
    : "this Supernote's own address is unknown";
  let message =
    `Couldn't find your computer on ${searched} (${self}). Make sure Supernote Clipboard is ` +
    'running and the computer is on the same Wi-Fi, or enter its address below.';
  if (unusualError) {
    message += ` Network error: ${describeNetworkError(unusualError)}`;
  }
  return new UserError(message, 'config');
}

/** Ask the computer to pair; the user clicks "Allow" in the Supernote Clipboard window. */
async function pair(url: string, progress: Progress): Promise<string> {
  const requestId = await requestPairing(url, await deviceName());
  const deadline = Date.now() + PAIRING_TIMEOUT_MS;
  let lastReminder = 0;
  while (Date.now() < deadline) {
    if (Date.now() - lastReminder >= PAIRING_REMINDER_MS) {
      progress('On your computer, click "Allow" in the Supernote Clipboard window.');
      lastReminder = Date.now();
    }
    await sleep(PAIRING_POLL_MS);
    const result = await pairingStatus(url, requestId);
    if (result.status === 'approved') {
      return result.token;
    }
    if (result.status === 'denied') {
      throw new UserError('The connection was denied on your computer.', 'config');
    }
    if (result.status === 'expired') {
      break;
    }
  }
  throw new UserError(
    'No answer from your computer. Try again and click "Allow" in the Supernote Clipboard window.',
    'config',
  );
}

/**
 * Return settings for a reachable, paired computer. Finds the computer on the Wi-Fi when its
 * address is unknown or has changed, and pairs when this Supernote is not paired yet.
 * - manualUrl: connect to this address instead of searching.
 * - search: search even if the saved address still answers.
 */
export async function connect({
  progress,
  manualUrl,
  search = false,
}: {
  progress: Progress;
  manualUrl?: string;
  search?: boolean;
}): Promise<Settings> {
  const saved = await loadSettings();
  await ensureInternet();

  let target: {url: string; info: ServerInfo} | null = null;
  if (manualUrl) {
    const result = await probe(manualUrl, saved.token, 4000);
    if (!result.info) {
      throw new UserError(
        `Supernote Clipboard is not answering at ${displayAddress(manualUrl)}: ${describeNetworkError(
          result.error,
        )}`,
        'config',
      );
    }
    target = {url: manualUrl, info: result.info};
  } else if (saved.serverUrl && !search) {
    const info = await hello(saved.serverUrl, saved.token);
    if (info && info.serverId === saved.serverId) {
      target = {url: saved.serverUrl, info};
    }
  }

  if (!target) {
    progress('Looking for your computer on this Wi-Fi...');
    const last = parseUrl(saved.serverUrl);
    const report = await findComputer(last.port, saved.token, saved.serverId, last.host);
    if (!report.found) {
      throw notFoundError(report);
    }
    target = report.found;
  }

  const {url, info} = target;
  if (info.serverId === saved.serverId && saved.token && info.paired) {
    if (url === saved.serverUrl) {
      return saved;
    }
    const moved = {...saved, serverUrl: url};
    await saveSettings(moved);
    return moved;
  }

  progress(`Found ${info.name}. Asking it to pair with this Supernote...`);
  const token = await pair(url, progress);
  const paired = {serverUrl: url, serverId: info.serverId, serverName: info.name, token};
  await saveSettings(paired);
  progress(`Paired with ${info.name}.`);
  return paired;
}
