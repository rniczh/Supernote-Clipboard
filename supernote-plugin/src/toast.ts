import {ToastAndroid} from 'react-native';

/** Android toast: visible over the note even when the plugin view is closed. */
export function toast(message: string) {
  try {
    ToastAndroid.showWithGravityAndOffset(
      message,
      ToastAndroid.SHORT,
      ToastAndroid.TOP,
      0,
      120,
    );
  } catch {
    // Host refused to show a toast; nothing else to do.
  }
}
