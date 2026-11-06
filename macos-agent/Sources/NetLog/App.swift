import AppKit
import SwiftUI

// NetLog — agent sieci w pasku menu.
//   NetLog                              normalne działanie
//   NETLOG_DIR=<katalog> NetLog         pliki CSV i dziennik w innym miejscu (testy)
//   APP_STORE=<plik> NetLog             inna baza SQLite (testy)
//   APP_SIMULATE_SLEEP=5,20 NetLog      po 5 s „uśpienie”, po kolejnych 20 s „wybudzenie”
//   NETLOG_NAS_IP / NETLOG_NAS_URL / NETLOG_INTERNET_IP  cele, jak w netlog.sh

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
        // Druga kopia pisałaby do tych samych plików — zostaje tylko pierwsza.
        let me = ProcessInfo.processInfo.processIdentifier
        if let id = Bundle.main.bundleIdentifier,
           NSRunningApplication.runningApplications(withBundleIdentifier: id).contains(where: { $0.processIdentifier != me }) {
            NSApp.terminate(nil)
            return
        }
        NSApp.setActivationPolicy(.accessory)
        Monitor.shared.start()
        Snapshot.scheduleIfAsked()
    }

    private var flushed = false

    /// Najpierw zapis bufora SQLite i zdarzenie STOP, potem właściwe wyjście. Nie używamy
    /// `.terminateLater`: AppKit czeka wtedy w modalnej pętli, w której zadania Swift nie ruszają.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if flushed { return .terminateNow }
        Task {
            await Monitor.shared.stop()
            flushed = true
            NSApp.terminate(nil)
        }
        return .terminateCancel
    }
}
