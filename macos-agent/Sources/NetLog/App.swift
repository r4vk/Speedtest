import AppKit
import SwiftUI

// NetLog — agent sieci w pasku menu.
//   NetLog                              normalne działanie
//   NETLOG_DIR=<katalog> NetLog         pliki CSV i dziennik w innym miejscu (testy)
//   APP_STORE=<plik> NetLog             inna baza SQLite (testy)
//   APP_SIMULATE_SLEEP=5,20 NetLog      po 5 s „uśpienie”, po kolejnych 20 s „wybudzenie”
//   NETLOG_NAS_IP / NETLOG_NAS_URL / NETLOG_INTERNET_IP  cele, jak w netlog.sh
//   NETLOG_DEBUG=1 NetLog               po 3 s wypisuje okna (w tym ikonę paska menu) na stderr

@main
struct NetLogApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    // @State jest makrem, którego wtyczka jest tylko w pełnym Xcode — tu wystarczy singleton @Observable.
    private let monitor = Monitor.shared

    var body: some Scene {
        MenuBarExtra {
            PopoverView()
                .environment(monitor)
        } label: {
            Image(nsImage: StatusIcon.image(for: monitor.health))
                .accessibilityLabel(StatusIcon.title(for: monitor.health))
        }
        .menuBarExtraStyle(.window)
        .commands {
            CommandGroup(replacing: .appSettings) {
                Button("Ustawienia…") { AppDelegate.shared?.showSettings() }
                    .keyboardShortcut(",")
            }
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    static weak var shared: AppDelegate?
    private var window: NSWindow?
    private var settingsWindow: NSWindow?

    func applicationWillFinishLaunching(_ notification: Notification) { Self.shared = self }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Start z „Przy logowaniu” po cichu; uruchomienie ręczne (Finder, Spotlight) otwiera okno.
        let lgit = AEKeyword(0x6C67_6974) // keyAELaunchedAsLogInItem
        let atLogin = NSAppleEventManager.shared().currentAppleEvent?.paramDescriptor(forKeyword: lgit)?.booleanValue ?? false
        // Druga kopia pisałaby do tych samych plików — zostaje tylko pierwsza. Przy restarcie (build.sh --open)
        // stara kopia może jeszcze zapisywać bufor, więc czekamy na nią do 3 s.
        // Wyjątek: tryb testowy z własną bazą (tools/snap.sh), który nie dotyka prawdziwych danych.
        let testMode = ProcessInfo.processInfo.environment["APP_STORE"] != nil
        NSApp.setActivationPolicy(.accessory)
        Task {
            if !testMode {
                for _ in 0..<30 where Self.otherInstanceRunning() { try? await Task.sleep(for: .milliseconds(100)) }
                if Self.otherInstanceRunning() { NSApp.terminate(nil); return }
            }
            Monitor.shared.start()
            Snapshot.scheduleIfAsked()
            if !atLogin && !testMode { showWindow() }
            // NETLOG_DEBUG=settings: od razu okno ustawień (sprawdzenie układu w prawdziwym oknie).
            if ProcessInfo.processInfo.environment["NETLOG_DEBUG"] == "settings" { showSettings() }
            if ProcessInfo.processInfo.environment["NETLOG_DEBUG"] != nil {
                try? await Task.sleep(for: .seconds(3))
                for w in NSApp.windows {
                    FileHandle.standardError.write(Data("DEBUG window \(type(of: w)) \(w.frame) visible=\(w.isVisible)\n".utf8))
                }
            }
        }
    }

    /// Ponowne uruchomienie działającej aplikacji (Finder, Spotlight, Launchpad) pokazuje okno —
    /// także wtedy, gdy macOS chowa ikonę w pasku menu.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showWindow()
        return false
    }

    /// Okno z tą samą treścią co popover. Póki jest otwarte, NetLog ma ikonę w Docku i w ⌘-Tab.
    func showWindow() {
        if window == nil {
            let w = NSWindow(contentViewController: NSHostingController(rootView: PopoverView().environment(Monitor.shared)))
            w.title = "NetLog"
            w.styleMask = [.titled, .closable, .miniaturizable]
            w.isReleasedWhenClosed = false
            w.delegate = self
            w.center()
            window = w
        }
        NSApp.setActivationPolicy(.regular)
        NSApp.activate()
        window?.makeKeyAndOrderFront(nil)
    }

    /// Okno ustawień, za każdym razem ze świeżym szkicem z bieżącej konfiguracji.
    func showSettings() {
        let draft = SettingsDraft(Monitor.shared.config)
        let view = SettingsView(draft: draft) { [weak self] in self?.settingsWindow?.close() }
            .environment(Monitor.shared)
        if let w = settingsWindow {
            let host = NSHostingController(rootView: view)
            host.sizingOptions = [.minSize, .preferredContentSize]
            w.contentViewController = host
        } else {
            let host = NSHostingController(rootView: view)
            host.sizingOptions = [.minSize, .preferredContentSize]
            let w = NSWindow(contentViewController: host)
            w.title = "Ustawienia NetLog"
            w.styleMask = [.titled, .closable, .resizable]
            w.isReleasedWhenClosed = false
            w.delegate = self
            w.center()
            settingsWindow = w
        }
        NSApp.setActivationPolicy(.regular)
        NSApp.activate()
        settingsWindow?.makeKeyAndOrderFront(nil)
    }

    /// Ikona w Docku tylko póki jakieś okno NetLog jest otwarte.
    func windowWillClose(_ notification: Notification) {
        let closing = notification.object as? NSWindow
        let others = [window, settingsWindow].compactMap { $0 }.filter { $0 !== closing && $0.isVisible }
        if others.isEmpty { NSApp.setActivationPolicy(.accessory) }
    }

    private static func otherInstanceRunning() -> Bool {
        let me = ProcessInfo.processInfo.processIdentifier
        guard let id = Bundle.main.bundleIdentifier else { return false }
        return NSRunningApplication.runningApplications(withBundleIdentifier: id)
            .contains { $0.processIdentifier != me && !$0.isTerminated }
    }

    /// Zapis bufora SQLite i zdarzenia STOP przed wyjściem, najwyżej 3 s. `.terminateLater` nie blokuje
    /// wylogowania ani restartu (w przeciwieństwie do `.terminateCancel`). Odpowiedź idzie przez
    /// CFRunLoopPerformBlock, bo AppKit czeka w modalnej pętli, a kolejka główna może być wtedy zajęta.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        let write = Monitor.shared.prepareStop()
        Task.detached {
            await withTaskGroup(of: Void.self) { group in
                group.addTask { await write() }
                group.addTask { try? await Task.sleep(for: .seconds(3)) }
                await group.next()
                group.cancelAll()
            }
            let main = CFRunLoopGetMain()
            let modes = [CFRunLoopMode.commonModes.rawValue, "NSModalPanelRunLoopMode" as CFString] as CFArray
            CFRunLoopPerformBlock(main, modes) {
                MainActor.assumeIsolated { NSApp.reply(toApplicationShouldTerminate: true) }
            }
            CFRunLoopWakeUp(main)
        }
        return .terminateLater
    }
}
