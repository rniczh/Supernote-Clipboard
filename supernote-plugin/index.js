/**
 * @format
 */

import {AppRegistry, Image} from 'react-native';
import {PluginManager} from 'sn-plugin-lib';
import App from './App';
import {name as appName} from './app.json';
import {BUTTON_BROWSE, BUTTON_PASTE, BUTTON_PHONE} from './src/constants';
import {pasteAndReport} from './src/paste';
import {openScreen} from './src/uiState';

AppRegistry.registerComponent(appName, () => App);

PluginManager.init();

const iconUri = asset => Image.resolveAssetSource(asset).uri;

// One tap, no plugin view: insert the newest screenshot or text from the computer.
PluginManager.registerButton(1, ['NOTE'], {
  id: BUTTON_PASTE,
  name: 'Paste from PC',
  icon: iconUri(require('./assets/icon_paste.png')),
  showType: 0,
});

// Opens the plugin view to pick an older item or change settings.
PluginManager.registerButton(1, ['NOTE'], {
  id: BUTTON_BROWSE,
  name: 'PC Clipboard',
  icon: iconUri(require('./assets/icon_list.png')),
  showType: 1,
});

// Shows a QR code; the phone that scans it can send a photo or copied text to this page.
PluginManager.registerButton(1, ['NOTE'], {
  id: BUTTON_PHONE,
  name: 'Paste from phone',
  icon: iconUri(require('./assets/icon_phone.png')),
  showType: 1,
});

PluginManager.registerConfigButton();

PluginManager.registerButtonListener({
  onButtonPress(event) {
    if (event.id === BUTTON_PASTE) {
      pasteAndReport();
    } else if (event.id === BUTTON_BROWSE) {
      openScreen('shots');
    } else if (event.id === BUTTON_PHONE) {
      openScreen('phone');
    }
  },
});

PluginManager.registerConfigButtonListener({
  onClick() {
    openScreen('settings');
    PluginManager.showPluginView();
  },
});
