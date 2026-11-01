# PC Clipboard for Supernote

Take a screenshot on your computer, tap **Paste screenshot** on your Supernote, and the image is inserted into the note you are writing in.

The project has two parts:

| Part | Where it runs | What it does |
| --- | --- | --- |
| `desktop/sn_clipboard.py` (**Supernote Clipboard**) | Your computer (macOS, Windows, Linux) | Screenshot tool + small local server that holds your recent screenshots |
| `supernote-plugin/` (**PC Clipboard**) | Supernote (NOTE app) | Toolbar buttons that download a screenshot from your computer and insert it into the page |

Both devices must be on the same Wi-Fi network.

## Download

Get the latest files from the [Releases](../../releases) page:

| File | For |
| --- | --- |
| `PCClipboard.snplg` | The Supernote plugin (install it as described in [step 2](#2-install-the-plugin-on-your-supernote)) |
| `SupernoteClipboard-macOS-<version>.zip` | The macOS menu bar app (Apple Silicon and Intel, macOS 13 or newer) |

Installing the macOS app:

1. Unzip it and drag **Supernote Clipboard.app** into **Applications**.
2. Open it. The app is not notarized by Apple, so macOS blocks it the first time ("Apple could not verify..."). Go to **System Settings → Privacy & Security**, scroll down and click **Open Anyway**, then open it again.
3. It needs Python 3. If macOS offers to install the command line developer tools, accept.
4. The first capture asks for **Screen Recording** permission: enable Supernote Clipboard in **System Settings → Privacy & Security → Screen Recording**, then quit and reopen the app.

On Windows and Linux, run `desktop/sn_clipboard.py` from the source code instead (see below).

## 1. Start Supernote Clipboard on your computer

Requires Python 3.8 or newer (macOS already has `python3`). No other installs are needed.

### macOS: menu bar app

Download it from Releases (see above), or build it from source:

```bash
desktop/macos/build-app.sh --install
```

This builds `Supernote Clipboard.app` (needs the Xcode command line tools for `swiftc`) and puts it in **/Applications**. Open it like any other app: it lives in the menu bar (camera icon) with **Capture Region**, **Open Window**, **Open at Login**, **Show Log** and **Quit**. The first capture asks for **Screen Recording** permission for Supernote Clipboard; allow it in System Settings → Privacy & Security, then quit and reopen the app.

The app is a small Swift shell (`desktop/macos/SupernoteClipboard.swift`) around a copy of `sn_clipboard.py`. To customize it, edit those files and run `build-app.sh --install` again. The build signs the app with a local certificate (created on the first build in `~/Library/Application Support/Supernote Clipboard/`), so the Screen Recording permission stays valid across rebuilds. If macOS keeps asking even though Supernote Clipboard is enabled, remove it from the Screen Recording list (or run `tccutil reset ScreenCapture com.rnic.supernote-clipboard`) and allow it again. Only run one copy at a time (quit the terminal version first), since both use port 8765.

### Any system: terminal

```bash
python3 desktop/sn_clipboard.py
```

On macOS you can also double-click `desktop/Supernote Clipboard.command`.

A browser window opens at `http://localhost:8765` (the macOS app opens it only until a Supernote is paired). Keep the app running while you take notes. There is nothing to copy over to the Supernote: it finds the computer by itself (see below).

Ways to get a screenshot into the app:

- **Capture shortcut**, like Xnip: press `Option + Shift + S` (macOS) or `Ctrl + Alt + S` (Windows) anywhere, drag over the area you want, and it is ready for your Supernote. Press `Esc` to cancel. The **Capture region** button in the window does the same.
- **Any screenshot copied to the clipboard** is picked up automatically, so your usual tool works too (Xnip, CleanShot, Snagit, ... when set to copy to the clipboard):
  - macOS: `Cmd + Ctrl + Shift + 4` (the `Ctrl` copies it to the clipboard)
  - Windows: `Win + Shift + S`
  - Linux: any tool that copies a PNG (requires `wl-clipboard` or `xclip`)
- Drag and drop an image onto the window, or press `Cmd/Ctrl + V` inside it.

The newest screenshot is marked **Next paste**. Click **Use next** on an older one to paste that instead.

To use a different shortcut, start the app once with `--hotkey`, for example `--hotkey cmd+shift+2` (modifiers `cmd`/`win`, `ctrl`, `alt`/`option`, `shift`, plus a letter or digit; `--hotkey off` disables it). The choice is remembered. Pick one that no other app uses. On Linux, bind `python3 /path/to/desktop/sn_clipboard.py capture` to a shortcut in your desktop settings instead.

Other commands: `sn_clipboard.py add image.png`, `sn_clipboard.py --port 9000`, `--no-watch` (turn off clipboard import), `--no-browser`.

Data lives in `~/.sn-clipboard/` (last 30 screenshots and the list of paired Supernotes).

## 2. Install the plugin on your Supernote

Your Supernote needs firmware with plugin support (Settings → Apps → Plugins).

1. Download `PCClipboard.snplg` from the [Releases](../../releases) page (or build it, see below). Connect the Supernote by USB (or use Browse & Access) and copy the file into the **MyStyle** folder.
2. On the Supernote: **Settings → Apps → Plugins → Add Plugin**, pick `PCClipboard.snplg` and install.
3. Open a note. The toolbar now has two buttons:
   - **Paste screenshot**: inserts the newest screenshot right away, without opening any screen.
   - **PC Screenshots**: shows your recent screenshots so you can pick one, plus **Settings**.

### First paste: one click on the computer, no typing

Tap **Paste screenshot**. The plugin searches your Wi-Fi for the computer running Supernote Clipboard, and the computer shows *"Supernote Manta wants to connect"* (with a notification on macOS). Click **Allow** once. The screenshot is pasted, and from then on every paste goes straight through.

If the computer gets a new IP address later (after a restart or on another network), the plugin finds it again by itself and stays paired. **Remove** in the computer window unpairs a Supernote.

If the computer is not found automatically (for example on networks that separate devices into different subnets), open **PC Screenshots → Settings** and enter the address shown in the computer window under "Not found automatically?".

The first time, the Supernote also asks for permissions. Choose **Always allow** so you are not asked every session:

- **Network**: to download screenshots from your computer.
- **File write / read**: downloaded screenshots are saved to `SCREENSHOT/PC Clipboard/` and inserted from there.

## Daily use

1. On the computer: press `Option + Shift + S` (Windows: `Ctrl + Alt + S`) and drag over the area.
2. On the Supernote: tap **Paste screenshot**.
3. Use the lasso to move or resize the image.

Screenshots are scaled on the computer to about 85% of the page width before they are sent.

## Troubleshooting

- **"Couldn't find your computer"**: check that the app is running and both devices are on the same Wi-Fi (not a guest network). On macOS allow incoming connections for Python if the firewall asks. Some routers block devices from talking to each other ("AP/client isolation"); then enter the address manually in Settings.
- **"No answer from your computer"**: the pairing request was not allowed within two minutes. Tap again and click **Allow** in the computer window.
- **"No longer paired"**: the Supernote was removed in the computer window. Tap **Find my computer** in Settings and allow it again.
- **The capture is only the desktop wallpaper (macOS)**: give your terminal (or Python) Screen Recording permission in System Settings → Privacy & Security.
- **The capture shortcut does nothing**: another app (for example Xnip itself) may use the same keys. Choose another one with `--hotkey`. The current shortcut is shown at the top of the window and in the terminal ("Capture: press ⌥⇧S anywhere").
- **Nothing happens on Paste**: open **PC Screenshots**; the reason is shown at the top.

## Security

- Only Supernotes you allowed in the computer window can download screenshots; each gets its own secret token, and **Remove** revokes it.
- Pairing requests can only be allowed from the computer itself. The control window (`http://localhost:8765`) only works on the computer itself.
- Traffic is plain HTTP on your local network. Use it on networks you trust.

## Building the plugin

Ready-made builds are on the [Releases](../../releases) page. To build it yourself (macOS):

```bash
./scripts/setup-toolchain.sh   # once: downloads JDK 17 + Android SDK into .toolchain/ (about 1 GB with the Gradle cache)
./scripts/build-plugin.sh      # type-checks, builds, copies to dist/PCClipboard.snplg
```

The plugin is a React Native 0.79.2 project using the official [`sn-plugin-lib`](https://docs.supernote.com/en) SDK (0.2.3), `react-native-fs` for local files, and two small Java modules: `PCNetworkInfo` reports the Supernote's own IP so it knows which subnet to search, and `PCHttp` talks to the computer over plain sockets (the Supernote plugin host blocks cleartext HTTP in `fetch`). Keep `pluginID` in `supernote-plugin/PluginConfig.json` unchanged, otherwise the Supernote treats a new build as a different plugin; bump `versionCode` / `versionName` for updates.

Layout:

```
desktop/sn_clipboard.py          computer app (server, capture, clipboard import, web UI)
desktop/macos/                   menu bar app: Swift shell, Info.plist, icon, build-app.sh
supernote-plugin/index.js        button registration
supernote-plugin/App.tsx         plugin screen (screenshot list, settings)
supernote-plugin/src/paste.ts       download + insert flow
supernote-plugin/src/connection.ts  find the computer, pair, remember it
supernote-plugin/src/discovery.ts   Wi-Fi subnet search
supernote-plugin/src/bridge.ts      HTTP client for the computer app
supernote-plugin/src/settings.ts    saved computer and pairing token
supernote-plugin/src/http.ts        requests through the native PCHttp module
supernote-plugin/android/app/src/main/java/com/pcclipboard/   PCNetworkInfo and PCHttp native modules
```

## Making a release

1. Bump the versions: `versionName` / `versionCode` in `supernote-plugin/PluginConfig.json` and `VERSION` in `desktop/sn_clipboard.py`.
2. Build both parts and zip the app (the files land in `dist/`, which is not committed):

   ```bash
   ./scripts/build-plugin.sh
   desktop/macos/build-app.sh
   ditto -c -k --norsrc --noextattr --keepParent "dist/Supernote Clipboard.app" "dist/SupernoteClipboard-macOS-<version>.zip"
   ```

3. On GitHub, open **Releases → Draft a new release**, create a tag such as `v1.2.0`, and attach `dist/PCClipboard.snplg` and the zip.

## License

[MIT](LICENSE)
