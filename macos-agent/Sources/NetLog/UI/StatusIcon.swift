import AppKit
import NetLogKit
import SwiftUI

/// Ikona w pasku menu: linia EKG. Szablonowa (jak systemowe) gdy wszystko gra; kolor tylko jako sygnał problemu.
enum StatusIcon {
    static func symbol(for h: Health) -> String {
        switch h {
        // Własny znak (linia EKG), żeby nie mylić z systemową ikoną Wi-Fi.
        case .ok, .starting, .lag: "waveform.path.ecg"
        case .stuck: "exclamationmark.triangle.fill"
        case .noIP: "wifi.slash"
        case .waking: "arrow.clockwise"
        case .sleeping: "moon.zzz"
        }
    }

    static func color(for h: Health) -> NSColor? {
        switch h {
        case .lag: .systemOrange
        case .stuck: .systemRed
        default: nil
        }
    }

    static func title(for h: Health) -> String {
        switch h {
        case .ok: "Sieć działa"
        case .lag: "Opóźnienia lub pojedyncze straty"
        case .stuck: "Zawieszenie: router nie odpowiada"
        case .noIP: "Brak adresu IP na Wi-Fi"
        case .waking: "Po wybudzeniu: Wi-Fi łączy się ponownie"
        case .sleeping: "Uśpienie: pomiary wstrzymane"
        case .starting: "Uruchamianie pomiarów"
        }
    }

    static func image(for h: Health) -> NSImage {
        let base = NSImage(systemSymbolName: symbol(for: h), accessibilityDescription: title(for: h)) ?? NSImage()
        var config = NSImage.SymbolConfiguration(pointSize: 15, weight: .regular)
        if let color = color(for: h) {
            config = config.applying(NSImage.SymbolConfiguration(paletteColors: [color]))
        }
        let img = base.withSymbolConfiguration(config) ?? base
        img.isTemplate = color(for: h) == nil
        return img
    }

    static func tint(for h: Health) -> Color {
        color(for: h).map(Color.init(nsColor:)) ?? (h == .ok ? .green : .secondary)
    }
}
