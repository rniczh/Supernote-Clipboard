import qrcode from 'qrcode-generator';
import React, {useCallback, useEffect, useMemo, useState} from 'react';
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
import {listClips, shotUrl, tokenHeaders, type Clip} from './src/bridge';
import {connect} from './src/connection';
import {toUserError} from './src/errors';
import {httpDownload} from './src/http';
import {pasteAndReport} from './src/paste';
import {
  PHONE_IDLE_MINUTES,
  startPhoneSession,
  stopPhoneSession,
  usePhoneSession,
} from './src/phone';
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
function ShotThumb({settings, shot}: {settings: Settings; shot: Clip}) {
  const [path, setPath] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const file = `${RNFS.CachesDirectoryPath}/thumbs/${shot.id}_w${THUMB_WIDTH}_g100.${shot.ext}`;
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

function TextPreview({clip}: {clip: Clip}) {
  const more = (clip.chars ?? 0) > (clip.preview ?? '').length ? '…' : '';
  return (
    <View style={styles.textThumb}>
      <Text style={styles.textThumbText} numberOfLines={8}>
        {clip.preview}
        {more}
      </Text>
    </View>
  );
}

function ShotsScreen({openCount}: {openCount: number}) {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [shots, setShots] = useState<Clip[] | null>(null);
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
      setShots(await listClips(current));
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

  const paste = (shot: Clip) => {
    setNotice(null);
    closeView();
    pasteAndReport(shot);
  };

  return (
    <View style={styles.flex}>
      <View style={styles.toolbar}>
        <Text style={styles.hint}>
          {loading
            ? 'Loading from your computer...'
            : 'Tap a screenshot or text to paste it into this page.'}
        </Text>
        <Button label="Refresh" onPress={load} disabled={loading} />
      </View>
      {shots && shots.length === 0 && !loading ? (
        <Text style={styles.empty}>
          Nothing yet. Take a screenshot or copy some text on your computer.
        </Text>
      ) : null}
      {settings && shots ? (
        <FlatList
          data={shots}
          keyExtractor={shot => shot.id}
          contentContainerStyle={styles.list}
          renderItem={({item, index}) => (
            <Pressable style={styles.shot} onPress={() => paste(item)}>
              {item.kind === 'text' ? (
                <TextPreview clip={item} />
              ) : (
                <ShotThumb settings={settings} shot={item} />
              )}
              <View style={styles.shotInfo}>
                <Text style={styles.shotTitle}>
                  {index === 0 ? 'Newest  ·  ' : ''}
                  {formatTime(item.createdAt)}
                </Text>
                <Text style={styles.shotMeta}>
                  {item.kind === 'text'
                    ? `Text · ${item.chars ?? 0} characters`
                    : `${item.width} × ${item.height} px`}
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

/** QR code drawn with plain Views: one black bar per run of dark modules in a row. */
function QrCode({value, size}: {value: string; size: number}) {
  const rows = useMemo(() => {
    const qr = qrcode(0, 'M');
    qr.addData(value);
    qr.make();
    const count = qr.getModuleCount();
    const result: {start: number; length: number}[][] = [];
    for (let r = 0; r < count; r++) {
      const runs: {start: number; length: number}[] = [];
      for (let c = 0; c < count; c++) {
        if (!qr.isDark(r, c)) {
          continue;
        }
        const last = runs[runs.length - 1];
        if (last && last.start + last.length === c) {
          last.length += 1;
        } else {
          runs.push({start: c, length: 1});
        }
      }
      result.push(runs);
    }
    return result;
  }, [value]);

  const quiet = 4;
  const cell = Math.max(4, Math.floor(size / (rows.length + 2 * quiet)));
  return (
    <View style={[styles.qr, {padding: quiet * cell}]}>
      <View style={{width: rows.length * cell, height: rows.length * cell}}>
        {rows.map((runs, r) =>
          runs.map(run => (
            <View
              key={`${r}-${run.start}`}
              style={[
                styles.qrDark,
                {top: r * cell, left: run.start * cell, width: run.length * cell, height: cell},
              ]}
            />
          )),
        )}
      </View>
    </View>
  );
}

function PhoneScreen({openCount}: {openCount: number}) {
  const session = usePhoneSession();

  useEffect(() => {
    if (session.status === 'off' || session.status === 'error' || session.status === 'starting') {
      startPhoneSession();
    }
    // Only on open; status changes while open must not restart the session.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openCount]);

  const live = session.status === 'waiting' || session.status === 'connected';
  return (
    <View style={styles.phone}>
      {session.status === 'starting' ? <Text style={styles.paragraph}>Starting...</Text> : null}
      {session.message ? <Text style={styles.paragraph}>{session.message}</Text> : null}
      {live && session.url ? (
        <>
          <QrCode value={session.url} size={520} />
          <Text style={styles.phoneUrl} selectable>
            {session.url}
          </Text>
          <Text style={styles.phoneStatus}>
            {session.status === 'connected'
              ? 'Phone connected. Choose a photo or paste on your phone; it appears on this page.'
              : 'Scan this code with your phone camera.'}
          </Text>
          <Text style={styles.paragraph}>
            The phone must be on the same Wi-Fi as this Supernote. On the page that opens, choose a
            photo, take one, or paste what you copied. This window closes and the item is placed on
            the current page. You can keep sending from the phone; the code stops working after{' '}
            {PHONE_IDLE_MINUTES} minutes without use.
          </Text>
        </>
      ) : null}
      <View style={styles.formActions}>
        <Button
          label="New code"
          primary={!live}
          disabled={session.status === 'starting'}
          onPress={startPhoneSession}
        />
        {live ? <Button label="Stop" onPress={() => stopPhoneSession('Stopped.')} /> : null}
      </View>
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
        text: `Connected to ${connected.serverName}. You can now use "Paste from PC".`,
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
        <Text style={styles.title}>PC Clipboard</Text>
        <View style={styles.tabs}>
          <Button
            label="Clipboard"
            primary={screen === 'shots'}
            onPress={() => openScreen('shots')}
          />
          <Button
            label="Phone"
            primary={screen === 'phone'}
            onPress={() => openScreen('phone')}
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
      {screen === 'shots' ? <ShotsScreen openCount={openCount} /> : null}
      {screen === 'phone' ? <PhoneScreen openCount={openCount} /> : null}
      {screen === 'settings' ? <SettingsScreen /> : null}
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
  textThumb: {
    width: THUMB_WIDTH,
    minHeight: 120,
    maxHeight: 360,
    padding: 14,
    borderWidth: 1,
    borderColor: '#888888',
    overflow: 'hidden',
  },
  textThumbText: {fontSize: 20, lineHeight: 28, color: '#000000'},
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
  phone: {paddingHorizontal: 28, paddingTop: 24, alignItems: 'center', gap: 12},
  qr: {backgroundColor: '#ffffff', borderWidth: 2, borderColor: '#000000'},
  qrDark: {position: 'absolute', backgroundColor: '#000000'},
  phoneUrl: {fontSize: 18, color: '#333333'},
  phoneStatus: {fontSize: 24, fontWeight: '700', color: '#000000', textAlign: 'center'},
});

export default App;
