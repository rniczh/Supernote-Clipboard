// Menu bar shell for Supernote Clipboard.
//
// All the real work (capture shortcut, clipboard import, server for the Supernote) is done by
// Resources/sn_clipboard.py, which this app runs as a child process. Being the parent gives the
// capture a proper app identity, so macOS asks for Screen Recording permission for
// "Supernote Clipboard" instead of Terminal.

import AppKit
import ServiceManagement

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private static let pythonCandidates = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]

    private let dataDir: URL = {
        if let custom = ProcessInfo.processInfo.environment["SN_CLIPBOARD_HOME"] {
            return URL(fileURLWithPath: custom)
        }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".sn-clipboard")
    }()
    private var logURL: URL { dataDir.appendingPathComponent("app.log") }

    private var statusItem: NSStatusItem!
    private var server: Process?
    private var quitting = false

    private lazy var captureItem = item("Capture Region", #selector(capture))
    private lazy var timeLimitItem = item("Time limit: Open Window to change it", #selector(openWindow))
    private lazy var statusLine = NSMenuItem(title: "Starting...", action: nil, keyEquivalent: "")
    private lazy var loginItem = item("Open at Login", #selector(toggleLogin))

    // MARK: - Lifecycle

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        let icon = NSImage(systemSymbolName: "camera.viewfinder", accessibilityDescription: "Supernote Clipboard")
        icon?.isTemplate = true
        statusItem.button?.image = icon

        let menu = NSMenu()
        menu.delegate = self
        for entry in [
            captureItem,
            item("Open Window", #selector(openWindow)),
            timeLimitItem,
            .separator(),
            statusLine,
            .separator(),
            loginItem,
            item("Show Log", #selector(showLog)),
            .separator(),
            item("Quit Supernote Clipboard", #selector(quit), key: "q"),
        ] {
            menu.addItem(entry)
        }
        statusItem.menu = menu

        startServer()
        if pairedCount() == 0 {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { self.openWindow() }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        quitting = true
        // Python exits on SIGTERM right away; waitUntilExit() would hang here on the main thread.
        server?.terminate()
    }

    // MARK: - Menu

    func menuWillOpen(_ menu: NSMenu) {
        loginItem.state = SMAppService.mainApp.status == .enabled ? .on : .off
        applyShortcut(config()["hotkey"] as? String ?? "")
        fetchState { state in
            guard let state = state else {
                self.statusLine.title = "Not running"
                return
            }
            if (state["hotkey"] as? String ?? "").isEmpty {
                self.applyShortcut("")
            }
            let paired = (state["devices"] as? [Any])?.count ?? 0
            let items = (state["shots"] as? [Any])?.count ?? 0
            let devices = paired == 0 ? "No Supernote paired" : "Paired with \(paired) Supernote\(paired == 1 ? "" : "s")"
            self.statusLine.title = "\(devices) · \(items) item\(items == 1 ? "" : "s") to paste"
            self.timeLimitItem.title = self.timeLimitTitle(state)
        }
    }

    @objc private func capture() {
        var request = URLRequest(url: localURL("/local/capture"))
        request.httpMethod = "POST"
        request.httpBody = Data("{}".utf8)
        request.setValue("1", forHTTPHeaderField: "X-SN-Local")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        URLSession.shared.dataTask(with: request).resume()
    }

    @objc private func openWindow() {
        NSWorkspace.shared.open(localURL("/", host: "localhost"))
    }

    @objc private func showLog() {
        NSWorkspace.shared.open(logURL)
    }

    @objc private func toggleLogin() {
        do {
            if SMAppService.mainApp.status == .enabled {
                try SMAppService.mainApp.unregister()
            } else {
                try SMAppService.mainApp.register()
            }
        } catch {
            alert("Could not change Open at Login", error.localizedDescription)
        }
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }

    // MARK: - Python server

    private func startServer() {
        guard let script = Bundle.main.url(forResource: "sn_clipboard", withExtension: "py") else {
            return fail("sn_clipboard.py is missing from the app. Rebuild it with desktop/macos/build-app.sh.")
        }
        guard let python = Self.pythonCandidates.first(where: FileManager.default.isExecutableFile) else {
            return fail("Python 3 was not found. Install it with `xcode-select --install` or from python.org.")
        }
        try? FileManager.default.createDirectory(at: dataDir, withIntermediateDirectories: true)
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        let log = try? FileHandle(forWritingTo: logURL)

        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = [script.path, "serve", "--app"]
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        process.environment = environment
        process.standardOutput = log
        process.standardError = log
        process.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async { self?.serverExited() }
        }
        do {
            try process.run()
            server = process
        } catch {
            fail("Could not start \(python): \(error.localizedDescription)")
        }
    }

    private func serverExited() {
        server = nil
        guard !quitting else { return }
        let tail = (try? String(contentsOf: logURL, encoding: .utf8))?
            .split(separator: "\n").suffix(4).joined(separator: "\n") ?? ""
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "Supernote Clipboard stopped"
        alert.informativeText = tail.isEmpty ? "See the log for details." : tail
        alert.addButton(withTitle: "Restart")
        alert.addButton(withTitle: "Quit")
        if alert.runModal() == .alertFirstButtonReturn {
            startServer()
        } else {
            NSApp.terminate(nil)
        }
    }

    private func fail(_ message: String) {
        alert("Supernote Clipboard cannot start", message)
        NSApp.terminate(nil)
    }

    // MARK: - Helpers

    private func item(_ title: String, _ action: Selector, key: String = "") -> NSMenuItem {
        let entry = NSMenuItem(title: title, action: action, keyEquivalent: key)
        entry.target = self
        return entry
    }

    private func alert(_ title: String, _ message: String) {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.runModal()
    }

    private func config() -> [String: Any] {
        guard let data = try? Data(contentsOf: dataDir.appendingPathComponent("config.json")),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [:] }
        return json
    }

    private func pairedCount() -> Int {
        (config()["devices"] as? [Any])?.count ?? 0
    }

    private func localURL(_ path: String, host: String = "127.0.0.1") -> URL {
        let port = config()["port"] as? Int ?? 8765
        return URL(string: "http://\(host):\(port)\(path)")!
    }

    private func fetchState(_ done: @escaping ([String: Any]?) -> Void) {
        var request = URLRequest(url: localURL("/local/state"), timeoutInterval: 2)
        request.setValue("1", forHTTPHeaderField: "X-SN-Local")
        URLSession.shared.dataTask(with: request) { data, _, _ in
            let state = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            DispatchQueue.main.async { done(state) }
        }.resume()
    }

    private func timeLimitTitle(_ state: [String: Any]) -> String {
        let limits = state["limits"] as? [String: Any]
        let seconds = integer(limits?["keep_seconds"]) ?? integer(limits?["keep_days"]).map { $0 * 86400 }
        let onPaste = state["deleteOnPaste"] as? Bool ?? false
        guard let seconds else { return "Time limit: Open Window to change it" }
        if seconds <= 0 {
            return onPaste ? "Time limit: until pasted" : "Time limit: no limit"
        }
        return onPaste ? "Time limit: \(formatDuration(seconds)), or when pasted" : "Time limit: \(formatDuration(seconds))"
    }

    private func integer(_ value: Any?) -> Int? {
        (value as? NSNumber)?.intValue
    }

    private func formatDuration(_ seconds: Int) -> String {
        let (count, unit): (Int, String)
        if seconds % 86400 == 0 {
            (count, unit) = (seconds / 86400, "day")
        } else if seconds % 3600 == 0 {
            (count, unit) = (seconds / 3600, "hour")
        } else if seconds % 60 == 0 {
            (count, unit) = (seconds / 60, "minute")
        } else {
            (count, unit) = (seconds, "second")
        }
        return count == 1 ? "1 \(unit)" : "\(count) \(unit)s"
    }

    /// Shows the capture shortcut ("alt+shift+s") next to the menu item.
    private func applyShortcut(_ text: String) {
        let parts = text.lowercased().split(separator: "+").map { $0.trimmingCharacters(in: .whitespaces) }
        guard parts.count >= 2, let key = parts.last, key.count == 1 else {
            captureItem.keyEquivalent = ""
            return
        }
        var mask: NSEvent.ModifierFlags = []
        for modifier in parts.dropLast() {
            switch modifier {
            case "cmd", "command", "win", "super": mask.insert(.command)
            case "ctrl", "control": mask.insert(.control)
            case "alt", "option", "opt": mask.insert(.option)
            case "shift": mask.insert(.shift)
            default: break
            }
        }
        captureItem.keyEquivalent = key
        captureItem.keyEquivalentModifierMask = mask
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
