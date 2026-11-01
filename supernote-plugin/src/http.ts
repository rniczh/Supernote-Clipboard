import {NativeModules} from 'react-native';

/**
 * HTTP to the computer goes through the native PCHttp module (plain sockets): the Supernote
 * plugin host blocks cleartext HTTP in fetch, Image and react-native-fs. fetch is only a fallback
 * for environments without the module.
 */
type NativeHttp = {
  request(
    url: string,
    method: string,
    headers: Record<string, string>,
    body: string | null,
    timeoutMs: number,
  ): Promise<{status: number; body: string}>;
  download(
    url: string,
    headers: Record<string, string>,
    path: string,
    timeoutMs: number,
  ): Promise<{status: number; body?: string}>;
};

export type NetworkErrorCode = 'E_TIMEOUT' | 'E_REFUSED' | 'E_UNREACHABLE' | 'E_HTTP';

export class NetworkError extends Error {
  constructor(message: string, readonly code: NetworkErrorCode) {
    super(message);
  }
}

export type HttpResponse = {status: number; body: any};

const nativeHttp: NativeHttp | undefined = NativeModules.PCHttp;

function parseJson(text: string | undefined) {
  try {
    return text ? JSON.parse(text) : {};
  } catch {
    return {};
  }
}

function toNetworkError(e: any): NetworkError {
  const code: NetworkErrorCode = ['E_TIMEOUT', 'E_REFUSED', 'E_UNREACHABLE'].includes(e?.code)
    ? e.code
    : e?.name === 'AbortError'
    ? 'E_TIMEOUT'
    : 'E_HTTP';
  const message = code === 'E_TIMEOUT' ? 'timed out' : String(e?.message || e || 'network error');
  return new NetworkError(message, code);
}

export async function httpRequest(
  url: string,
  {
    method = 'GET',
    headers = {},
    body,
    timeoutMs,
  }: {method?: string; headers?: Record<string, string>; body?: unknown; timeoutMs: number},
): Promise<HttpResponse> {
  const allHeaders: Record<string, string> = {Accept: 'application/json', ...headers};
  const text = body === undefined ? null : JSON.stringify(body);
  if (text !== null) {
    allHeaders['Content-Type'] = 'application/json';
  }
  if (nativeHttp) {
    try {
      const response = await nativeHttp.request(url, method, allHeaders, text, timeoutMs);
      return {status: response.status, body: parseJson(response.body)};
    } catch (e) {
      throw toNetworkError(e);
    }
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      method,
      headers: allHeaders,
      body: text ?? undefined,
      signal: controller.signal,
    });
    return {status: response.status, body: parseJson(await response.text())};
  } catch (e) {
    throw toNetworkError(e);
  } finally {
    clearTimeout(timer);
  }
}

/** Download url to path. Resolves the HTTP status (the file only exists for 200) and the error body. */
export async function httpDownload(
  url: string,
  headers: Record<string, string>,
  path: string,
  timeoutMs: number,
): Promise<HttpResponse> {
  if (!nativeHttp) {
    throw new NetworkError('downloads need the PCHttp native module', 'E_HTTP');
  }
  try {
    const response = await nativeHttp.download(url, headers, path, timeoutMs);
    return {status: response.status, body: parseJson(response.body)};
  } catch (e) {
    throw toNetworkError(e);
  }
}
