import AppKit
import SwiftUI

/// `APP_SNAPSHOT=<katalog> APP_SNAPSHOT_AFTER=<s>`: po zebraniu danych rysuje popover poza ekranem
/// w trybie jasnym i ciemnym, zapisuje `popover-light.png` / `popover-dark.png` i kończy pracę.
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
                let file = url.appending(path: "popover-\(name).png")
                render(appearance: appearance, to: file)
                print("SNAPSHOT \(file.path)")
            }
            NSApp.terminate(nil)
        }
    }

    private static func render(appearance: NSAppearance.Name, to file: URL) {
        let scheme: ColorScheme = appearance == .darkAqua ? .dark : .light
        let host = NSHostingView(rootView: PopoverView()
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
        FileHandle.standardError.write(Data("SNAPSHOT size \(size)\n".utf8))
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
