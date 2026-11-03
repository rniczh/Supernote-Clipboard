import {NativeModules} from 'react-native';
import {probe, type ServerInfo} from './bridge';
import {NetworkError} from './http';

type LocalAddress = {address: string; prefixLength: number; interface: string};

export type FoundServer = {url: string; info: ServerInfo};

/** What the search did, for explaining a failure. */
export type SearchReport = {
  found: FoundServer | null;
  ownAddresses: string[];
  subnets: string[];
  /** The most common failure other than "nothing there" (timeouts and refused connections). */
  unusualError: unknown;
};

const CONCURRENCY = 48;
const PROBE_TIMEOUT_MS = 1500;
/** Used only when the device's own address is unknown: the most common home subnets. */
const FALLBACK_SUBNETS = ['192.168.0', '192.168.1', '192.168.2', '10.0.0', '10.0.1'];

/** IPv4 addresses of this Supernote, Wi-Fi interfaces first (native PCNetworkInfo module). */
export async function localAddresses(): Promise<LocalAddress[]> {
  const module = NativeModules.PCNetworkInfo;
  if (!module?.getIPv4Addresses) {
    return [];
  }
  try {
    const list: LocalAddress[] = await module.getIPv4Addresses();
    return list.sort(
      (a, b) => Number(b.interface.startsWith('wlan')) - Number(a.interface.startsWith('wlan')),
    );
  } catch {
    return [];
  }
}

function subnetOf(ip: string) {
  return ip.split('.').slice(0, 3).join('.');
}

/** Hosts to probe, most likely first: the last known address, then every host of the local /24s. */
async function candidateHosts(lastHost: string) {
  const own = await localAddresses();
  const ownIps = new Set(own.map(a => a.address));
  const subnets = [
    ...new Set(
      own.length
        ? own.map(a => subnetOf(a.address))
        : [...(lastHost ? [subnetOf(lastHost)] : []), ...FALLBACK_SUBNETS],
    ),
  ];

  const hosts = new Set<string>(lastHost ? [lastHost] : []);
  for (const subnet of subnets) {
    for (let i = 1; i < 255; i++) {
      const ip = `${subnet}.${i}`;
      if (!ownIps.has(ip)) {
        hosts.add(ip);
      }
    }
  }
  return {hosts: [...hosts], ownAddresses: [...ownIps], subnets};
}

function isNothingThere(error: unknown) {
  return error instanceof NetworkError && (error.code === 'E_TIMEOUT' || error.code === 'E_REFUSED');
}

/**
 * Probe the local network for Supernote Clipboard. Stops early at the computer with
 * preferredServerId, or at the first computer found when there is no preference.
 */
export async function findComputer(
  port: number,
  token: string,
  preferredServerId: string,
  lastHost: string,
): Promise<SearchReport> {
  const {hosts: queue, ownAddresses, subnets} = await candidateHosts(lastHost);
  const found: FoundServer[] = [];
  const unusual = new Map<string, {error: unknown; count: number}>();
  let done = false;

  const worker = async () => {
    while (!done && queue.length) {
      const host = queue.shift()!;
      const url = `http://${host}:${port}`;
      const {info, error} = await probe(url, token, PROBE_TIMEOUT_MS);
      if (info) {
        found.push({url, info});
        if (!preferredServerId || info.serverId === preferredServerId) {
          done = true;
        }
      } else if (!isNothingThere(error)) {
        const key = String((error as any)?.message ?? error);
        const entry = unusual.get(key) ?? {error, count: 0};
        entry.count++;
        unusual.set(key, entry);
      }
    }
  };
  await Promise.all(Array.from({length: CONCURRENCY}, worker));

  const mostCommon = [...unusual.values()].sort((a, b) => b.count - a.count)[0];
  return {
    found: found.find(f => f.info.serverId === preferredServerId) ?? found[0] ?? null,
    ownAddresses,
    subnets,
    unusualError: mostCommon?.error ?? null,
  };
}
