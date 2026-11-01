import React, {useCallback, useEffect, useState} from 'react';
import {
  FlatList,
  Image,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import RNFS from 'react-native-fs';
import {PluginManager} from 'sn-plugin-lib';
import {listShots, shotUrl, tokenHeaders, type Shot} from './src/bridge';
import {connect} from './src/connection';
import {toUserError} from './src/errors';
import {httpDownload} from './src/http';
import {pasteAndReport} from './src/paste';
import {
  displayAddress,
  EMPTY_SETTINGS,
  loadSettings,
  normalizeServerUrl,
  saveSettings,
  type Settings,
} from './src/settings';
import {openScreen, setNotice, useUiState, type Notice} from './src/uiState';

const THUMB_WIDTH = 360;

function closeView() {
  PluginManager.closePluginView().catch(() => {});
}

function Button({
  label,
  onPress,
  primary,
  disabled,
}: {
  label: string;
  onPress: () => void;
  primary?: boolean;
  disabled?: boolean;
}) {
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled}
      style={[styles.button, primary && styles.buttonPrimary, disabled && styles.buttonDisabled]}>
      <Text style={[styles.buttonText, primary && styles.buttonTextPrimary]}>{label}</Text>
    </Pressable>
  );
}

function NoticeBanner({notice}: {notice: Notice | null}) {
  if (!notice) {
    return null;
  }
  return (
    <View style={[styles.notice, notice.error && styles.noticeError]}>
      <Text style={styles.noticeText}>{notice.text}</Text>
    </View>
  );
}

function pad(n: number) {
  return String(n).padStart(2, '0');
}

function formatTime(ms: number) {
  const d = new Date(ms);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(
    d.getHours(),
  )}:${pad(d.getMinutes())}`;
}

/** Thumbnail cached on the Supernote; Image cannot load it over HTTP from the computer itself. */
function ShotThumb({settings, shot}: {settings: Settings; shot: Shot}) {
  const [path, setPath] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const file = `${RNFS.CachesDirectoryPath}/thumbs/${shot.id}_w${THUMB_WIDTH}.${shot.ext}`;
    (async () => {
      if (!(await RNFS.exists(file))) {
        const {status} = await httpDownload(
          shotUrl(settings, shot, THUMB_WIDTH),
          tokenHeaders(settings),
          `${file}.part`,
          15000,
        );
        if (status !== 200) {
          return;
        }
        await RNFS.moveFile(`${file}.part`, file);
      }
      if (!cancelled) {
        setPath(file);
      }
    })().catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [settings, shot]);

  const style = [styles.thumb, {aspectRatio: Math.max(shot.width / Math.max(shot.height, 1), 0.5)}];
  return path ? (
    <Image source={{uri: `file://${path}`}} resizeMode="contain" style={style} fadeDuration={0} />
  ) : (
    <View style={style} />
  );
}

function ShotsScreen({openCount}: {openCount: number}) {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [shots, setShots] = useState<Shot[] | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    let showedProgress = false;
    try {
      const current = await connect({
        progress: text => {
          showedProgress = true;
          setNotice({text, error: false});
        },
      });
      if (showedProgress) {
        setNotice(null);
      }
      setSettings(current);
      setShots(await listShots(current));
    } catch (e) {
      const error = toUserError(e);
      setShots(null);
      if (error.kind === 'config') {
        openScreen('settings', {text: error.message, error: true});
      } else {
        setNotice({text: error.message, error: true});
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load, openCount]);

  const paste = (shot: Shot) => {
    setNotice(null);
    closeView();
    pasteAndReport(shot);
  };

  return (
    <View style={styles.flex}>
      <View style={styles.toolbar}>
        <Text style={styles.hint}>
          {loading
            ? 'Loading screenshots from your computer...'
            : 'Tap a screenshot to paste it into this page.'}
        </Text>
        <Button label="Refresh" onPress={load} disabled={loading} />
      </View>
      {shots && shots.length === 0 && !loading ? (
        <Text style={styles.empty}>
          No screenshots yet. Take one on your computer with Supernote Clipboard.
        </Text>
      ) : null}
      {settings && shots ? (
        <FlatList
          data={shots}
          keyExtractor={shot => shot.id}
          contentContainerStyle={styles.list}
          renderItem={({item, index}) => (
            <Pressable style={styles.shot} onPress={() => paste(item)}>
              <ShotThumb settings={settings} shot={item} />
              <View style={styles.shotInfo}>
                <Text style={styles.shotTitle}>
                  {index === 0 ? 'Newest  ·  ' : ''}
                  {formatTime(item.createdAt)}
                </Text>
                <Text style={styles.shotMeta}>
                  {item.width} × {item.height} px
                </Text>
                <View style={styles.shotAction}>
                  <Button label="Paste" primary onPress={() => paste(item)} />
                </View>
              </View>
            </Pressable>
          )}
        />
      ) : null}
    </View>
  );
}

function SettingsScreen() {
  const [settings, setSettings] = useState<Settings>(EMPTY_SETTINGS);
  const [address, setAddress] = useState('');
  const [working, setWorking] = useState(false);

  useEffect(() => {
    loadSettings().then(setSettings);
  }, []);

  const run = async (options: {search?: boolean; manualUrl?: string}) => {
    setWorking(true);
    try {
      const connected = await connect({
        ...options,
        progress: text => setNotice({text, error: false}),
      });
      setSettings(connected);
      setNotice({
        text: `Connected to ${connected.serverName}. You can now use "Paste screenshot".`,
        error: false,
      });
    } catch (e) {
      setNotice({text: toUserError(e).message, error: true});
    } finally {
      setWorking(false);
    }
  };

  const connectManually = () => {
    const manualUrl = normalizeServerUrl(address);
    if (!manualUrl) {
      setNotice({text: 'Enter an address like 192.168.1.20:8765.', error: true});
      return;
    }
    setAddress(displayAddress(manualUrl));
    run({manualUrl});
  };

  const forget = async () => {
    await saveSettings(EMPTY_SETTINGS);
    setSettings(EMPTY_SETTINGS);
    setNotice({text: 'Forgot the computer. It will be searched for again on the next paste.', error: false});
  };

  const paired = !!settings.token;

  return (
    <View style={styles.form}>
      <Text style={styles.label}>Computer</Text>
      <Text style={styles.paragraph}>
        {paired
          ? `Paired with ${settings.serverName} (${displayAddress(settings.serverUrl)}).`
          : 'Not paired yet. Start Supernote Clipboard on your computer and tap "Find my computer". ' +
            'Both devices must be on the same Wi-Fi.'}
      </Text>
      <View style={styles.formActions}>
        <Button
          label={working ? 'Working...' : 'Find my computer'}
          primary
          disabled={working}
          onPress={() => run({search: true})}
        />
        {paired ? <Button label="Forget" disabled={working} onPress={forget} /> : null}
      </View>

      <Text style={[styles.label, styles.section]}>Not found automatically?</Text>
      <Text style={styles.paragraph}>
        Enter the address shown in the Supernote Clipboard window on your computer.
      </Text>
      <View style={styles.formActions}>
        <TextInput
          style={[styles.input, styles.flex]}
          value={address}
          onChangeText={setAddress}
          placeholder="192.168.1.20:8765"
          placeholderTextColor="#888"
          autoCapitalize="none"
          autoCorrect={false}
          keyboardType="url"
        />
        <Button label="Connect" disabled={working} onPress={connectManually} />
      </View>
    </View>
  );
}

function App(): React.JSX.Element {
  const {screen, notice, openCount} = useUiState();

  return (
    <View style={styles.container}>
      <View style={styles.header}>
        <Text style={styles.title}>PC Screenshots</Text>
        <View style={styles.tabs}>
          <Button
            label="Screenshots"
            primary={screen === 'shots'}
            onPress={() => openScreen('shots')}
          />
          <Button
            label="Settings"
            primary={screen === 'settings'}
            onPress={() => openScreen('settings')}
          />
          <Button label="Close" onPress={closeView} />
        </View>
      </View>
      <NoticeBanner notice={notice} />
      {screen === 'shots' ? <ShotsScreen openCount={openCount} /> : <SettingsScreen />}
    </View>
  );
}

const styles = StyleSheet.create({
  flex: {flex: 1},
  container: {flex: 1, backgroundColor: '#ffffff'},
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingHorizontal: 28,
    paddingVertical: 18,
    borderBottomWidth: 2,
    borderColor: '#000000',
  },
  title: {flex: 1, fontSize: 30, fontWeight: '700', color: '#000000'},
  tabs: {flexDirection: 'row', gap: 12},
  button: {
    borderWidth: 2,
    borderColor: '#000000',
    borderRadius: 10,
    paddingHorizontal: 22,
    paddingVertical: 12,
    backgroundColor: '#ffffff',
  },
  buttonPrimary: {backgroundColor: '#000000'},
  buttonDisabled: {opacity: 0.4},
  buttonText: {fontSize: 22, color: '#000000', fontWeight: '600'},
  buttonTextPrimary: {color: '#ffffff'},
  notice: {
    marginHorizontal: 28,
    marginTop: 18,
    padding: 16,
    borderWidth: 2,
    borderColor: '#000000',
    borderRadius: 10,
  },
  noticeError: {borderWidth: 4},
  noticeText: {fontSize: 22, color: '#000000'},
  toolbar: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingHorizontal: 28,
    paddingVertical: 16,
    gap: 16,
  },
  hint: {flex: 1, fontSize: 22, color: '#333333'},
  empty: {fontSize: 24, color: '#000000', textAlign: 'center', marginTop: 80, paddingHorizontal: 40},
  list: {paddingHorizontal: 28, paddingBottom: 40, gap: 18},
  shot: {
    flexDirection: 'row',
    borderWidth: 2,
    borderColor: '#000000',
    borderRadius: 12,
    padding: 14,
    gap: 20,
  },
  thumb: {width: THUMB_WIDTH, maxHeight: 360, backgroundColor: '#f2f2f2'},
  shotInfo: {flex: 1, justifyContent: 'center', gap: 8},
  shotTitle: {fontSize: 24, fontWeight: '700', color: '#000000'},
  shotMeta: {fontSize: 20, color: '#333333'},
  shotAction: {flexDirection: 'row', marginTop: 8},
  form: {paddingHorizontal: 28, paddingTop: 24, gap: 12},
  paragraph: {fontSize: 22, color: '#333333', marginBottom: 12, lineHeight: 32},
  label: {fontSize: 22, fontWeight: '700', color: '#000000', marginTop: 8},
  input: {
    borderWidth: 2,
    borderColor: '#000000',
    borderRadius: 10,
    fontSize: 26,
    paddingHorizontal: 16,
    paddingVertical: 12,
    color: '#000000',
  },
  formActions: {flexDirection: 'row', alignItems: 'center', gap: 12, marginTop: 8},
  section: {marginTop: 40},
});

export default App;
