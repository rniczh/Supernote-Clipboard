import {PluginManager} from 'sn-plugin-lib';
import {UserError} from './errors';

/**
 * Check, and if needed request, a plugin permission.
 * "Allow this time only" expires when the plugin session ends, so this must run
 * before every operation that needs the permission, not just once.
 */
export async function ensurePermission(permission: string, reason: string) {
  let granted: boolean;
  try {
    if ((await PluginManager.hasPermission(permission)) === 1) {
      return;
    }
    const choice = await PluginManager.requestPermission(permission, reason);
    granted = choice === 1 || choice === 2;
  } catch {
    // Firmware without the permission system: nothing to request.
    return;
  }
  if (!granted) {
    throw new UserError(
      `Permission was not granted. ${reason} You can allow it in Settings > Apps > Plugins.`,
    );
  }
}
