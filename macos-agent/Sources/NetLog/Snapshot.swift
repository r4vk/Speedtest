import AppKit
import SwiftUI

/// `APP_SNAPSHOT=<katalog> APP_SNAPSHOT_AFTER=<s>`: po zebraniu danych rysuje popover poza ekranem
/// i okno ustawień w trybie jasnym i ciemnym (`popover-*.png`, `settings-*.png`) i kończy pracę.
/// Używać razem z `NETLOG_DIR` i `APP_STORE` w katalogu tymczasowym (tools/snap.sh).
enum Snapshot {
    static func scheduleIfAsked() {
        let env = ProcessInfo.processInfo.environment
        guard let dir = env["APP_SNAPSHOT"] else { return }
        let after = Double(env["APP_SNAPSHOT_AFTER"] ?? "") ?? 15
        Task {
            try? await Task.sleep(for: .seconds(after))
            let url = URL(filePath: dir, directoryHint: .isDirectory)
            try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
            for (name, appearance) in [("light", NSAppearance.Name.aqua), ("dark", .darkAqua)] {
                let pages: [(String, AnyView)] = [
                    ("popover", AnyView(PopoverView())),
                    ("settings", AnyView(SettingsView(draft: SettingsDraft(Monitor.shared.config)) {})),
                ]
                for (page, view) in pages {
                    let file = url.appending(path: "\(page)-\(name).png")
                    render(view, appearance: appearance, to: file)
                    print("SNAPSHOT \(file.path)")
                }
            }
            NSApp.terminate(nil)
        }
    }

    private static func render(_ view: AnyView, appearance: NSAppearance.Name, to file: URL) {
        let scheme: ColorScheme = appearance == .darkAqua ? .dark : .light
        let host = NSHostingView(rootView: view
            .environment(Monitor.shared)
            .background(Color(nsColor: .windowBackgroundColor))
            .environment(\.colorScheme, scheme))
        host.appearance = NSAppearance(named: appearance)
        let size = host.fittingSize
        let window = NSWindow(contentRect: NSRect(origin: .zero, size: size), styleMask: [.borderless], backing: .buffered, defer: false)
        window.appearance = NSAppearance(named: appearance)
        window.backgroundColor = .windowBackgroundColor
        window.contentView = host
        host.frame = NSRect(origin: .zero, size: size)
        host.layoutSubtreeIfNeeded()
        host.display()
        guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else {
            FileHandle.standardError.write(Data("SNAPSHOT no bitmap\n".utf8))
            return
        }
        host.cacheDisplay(in: host.bounds, to: rep)
        let out = NSImage(size: size)
        out.addRepresentation(rep)
        if let tiff = out.tiffRepresentation, let png = NSBitmapImageRep(data: tiff)?.representation(using: .png, properties: [:]) {
            do { try png.write(to: file) } catch { FileHandle.standardError.write(Data("SNAPSHOT \(error)\n".utf8)) }
        } else {
            FileHandle.standardError.write(Data("SNAPSHOT no png\n".utf8))
        }
    }
}
