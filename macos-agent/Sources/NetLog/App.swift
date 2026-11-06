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
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
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
            if ProcessInfo.processInfo.environment["NETLOG_DEBUG"] != nil {
                try? await Task.sleep(for: .seconds(3))
                for w in NSApp.windows {
                    FileHandle.standardError.write(Data("DEBUG window \(type(of: w)) \(w.frame) visible=\(w.isVisible)\n".utf8))
                }
            }
        }
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
