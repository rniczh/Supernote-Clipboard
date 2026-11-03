import {UserError} from './errors';
import {httpRequest, NetworkError} from './http';
import {displayAddress, type Settings} from './settings';

const APP_NAME = 'Supernote Clipboard';

/** An image or a piece of text held by the Supernote Clipboard app on the computer. */
export type Clip = {
  id: string;
  kind: 'image' | 'text';
  ext: 'png' | 'jpg' | 'txt';
  width: number;
  height: number;
  createdAt: number;
  source: string;
  /** Text only: length and the first few hundred characters. */
  chars?: number;
  preview?: string;
};

export type ServerInfo = {
  serverId: string;
  name: string;
  /** Whether the token sent with the request belongs to a paired device. */
  paired: boolean;
};

export type PairingStatus =
  | {status: 'pending' | 'denied' | 'expired'}
  | {status: 'approved'; token: string};

const SHOT_ID_RE = /^\d{8}-\d{6}-[0-9a-f]{4}$/;
const REQUEST_TIMEOUT_MS = 6000;

/** Validates a clip from the computer. */
function toClip(value: any): Clip | null {
  if (!value || typeof value.id !== 'string' || !SHOT_ID_RE.test(value.id)) {
    return null;
  }
  const kind = value.kind ?? 'image';
  const valid =
    kind === 'text'
      ? value.ext === 'txt'
      : kind === 'image' && (value.ext === 'png' || value.ext === 'jpg');
  return valid ? {...value, kind} : null;
}

/** Why a request did not reach Supernote Clipboard, in words for the notice banner. */
export function describeNetworkError(e: unknown): string {
  if (!(e instanceof NetworkError)) {
    return String((e as any)?.message || e);
  }
  switch (e.code) {
    case 'E_REFUSED':
      return 'nothing is listening there. Is Supernote Clipboard running on the computer?';
    case 'E_TIMEOUT':
      return 'no response. Are both devices on the same Wi-Fi?';
    case 'E_UNREACHABLE':
      return "that address is not reachable from this Supernote's network";
    default:
      return e.message;
  }
}

export function unreachableError(settings: Settings, cause?: unknown) {
  return new UserError(
    `Cannot reach ${settings.serverName || 'your computer'} at ${displayAddress(settings.serverUrl)}: ${
      cause
        ? describeNetworkError(cause)
        : 'make sure Supernote Clipboard is running and both devices are on the same Wi-Fi.'
    }`,
    'network',
  );
}

function request(
  url: string,
  init: {method?: string; token?: string; body?: unknown; timeoutMs?: number} = {},
) {
  return httpRequest(url, {
    method: init.method,
    headers: init.token ? {'X-SN-Token': init.token} : {},
    body: init.body,
    timeoutMs: init.timeoutMs ?? REQUEST_TIMEOUT_MS,
  });
}

export type ProbeResult = {info: ServerInfo; error?: undefined} | {info?: undefined; error: unknown};

/** Identify a Supernote Clipboard server at baseUrl. Never throws. */
export async function probe(baseUrl: string, token = '', timeoutMs = 2500): Promise<ProbeResult> {
  try {
    const {status, body} = await request(`${baseUrl}/api/hello`, {token, timeoutMs});
    if (status !== 200 || body?.app !== APP_NAME || typeof body.serverId !== 'string') {
      return {error: new Error(`another program answered (HTTP ${status}), not Supernote Clipboard`)};
    }
    return {
      info: {serverId: body.serverId, name: String(body.name || 'Computer'), paired: !!body.paired},
    };
  } catch (e) {
    return {error: e};
  }
}

/** Like probe, but null means "not there". */
export async function hello(baseUrl: string, token = '', timeoutMs = 2500) {
  return (await probe(baseUrl, token, timeoutMs)).info ?? null;
}

export async function requestPairing(baseUrl: string, deviceName: string): Promise<string> {
  let result;
  try {
    result = await request(`${baseUrl}/api/pair`, {method: 'POST', body: {deviceName}});
  } catch (e) {
    throw new UserError(
      `Lost the connection to your computer while pairing: ${describeNetworkError(e)}`,
      'network',
    );
  }
  if (result.status !== 200 || typeof result.body?.requestId !== 'string') {
    throw new UserError(result.body?.error || 'Your computer did not accept the pairing request.');
  }
  return result.body.requestId;
}

/** Network hiccups count as "pending" so a short Wi-Fi drop does not abort pairing. */
export async function pairingStatus(baseUrl: string, requestId: string): Promise<PairingStatus> {
  try {
    const {status, body} = await request(`${baseUrl}/api/pair/${requestId}`);
    if (status === 404) {
      return {status: 'expired'};
    }
    if (body?.status === 'approved' && typeof body.token === 'string') {
      return {status: 'approved', token: body.token};
    }
    if (body?.status === 'denied') {
      return {status: 'denied'};
    }
  } catch {
    // Treat as still pending.
  }
  return {status: 'pending'};
}

async function getJson(settings: Settings, path: string) {
  let result;
  try {
    result = await request(settings.serverUrl + path, {token: settings.token});
  } catch (e) {
    throw unreachableError(settings, e);
  }
  if (result.status === 401) {
    throw new UserError(
      'This Supernote is no longer paired. Open Settings and tap "Find my computer".',
      'config',
    );
  }
  return result;
}

function httpError(status: number, body: any) {
  return new UserError(body?.error || `Your computer answered with HTTP ${status}.`, 'network');
}

const KINDS = 'kinds=image,text';

/** Newest image or text, or null when the computer has nothing yet. */
export async function getLatestClip(settings: Settings): Promise<Clip | null> {
  const {status, body} = await getJson(settings, `/api/latest?${KINDS}`);
  if (status === 404) {
    return null;
  }
  if (status !== 200) {
    throw httpError(status, body);
  }
  return toClip(body?.shot);
}

export async function listClips(settings: Settings): Promise<Clip[]> {
  const {status, body} = await getJson(settings, `/api/shots?${KINDS}`);
  if (status !== 200) {
    throw httpError(status, body);
  }
  const clips = Array.isArray(body?.shots) ? body.shots.map(toClip) : [];
  return clips.filter((clip: Clip | null): clip is Clip => clip !== null);
}

export async function getClipText(settings: Settings, clip: Clip): Promise<string> {
  const {status, body} = await getJson(settings, `/api/text/${clip.id}`);
  if (status !== 200 || typeof body?.text !== 'string') {
    throw httpError(status, body);
  }
  return body.text;
}

export function tokenHeaders(settings: Settings): Record<string, string> {
  return {'X-SN-Token': settings.token};
}

/** Image URL: the computer scales the image down when it is wider than maxWidth. */
export function shotUrl(settings: Settings, clip: Clip, maxWidth?: number) {
  const query = maxWidth ? `?maxWidth=${Math.round(maxWidth)}` : '';
  return `${settings.serverUrl}/api/shots/${clip.id}.${clip.ext}${query}`;
}
