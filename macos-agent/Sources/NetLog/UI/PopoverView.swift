import Charts
import NetLogKit
import ServiceManagement
import SwiftUI

/// Popover z paska menu: stan, trzy cele, wykres 5 minut, Wi-Fi, ostatnie zdarzenia.
struct PopoverView: View {
    @Environment(Monitor.self) private var monitor

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            if monitor.legacyLoggerRunning {
                Label("Działa też stary logger powłoki (org.r4vk.netlog) i pisze do tych samych plików. Uruchom tools/macos-netlog/uninstall.sh.",
                      systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
            if monitor.localNetworkBlocked {
                Label("macOS blokuje dostęp do NAS-a (sieć lokalna). Włącz NetLog w Ustawienia systemowe > Prywatność i ochrona > Sieć lokalna.",
                      systemImage: "lock.shield")
                    .font(.caption)
                    .foregroundStyle(.red)
            }
            if let err = monitor.storeError {
                Label("Baza SQLite niedostępna: \(err). CSV zapisują się dalej.", systemImage: "externaldrive.badge.exclamationmark")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
            targets
            LatencyChart(samples: monitor.samples)
                .frame(height: 140)
            wifi
            Divider()
            recentEvents
            Divider()
            footer
        }
        .padding(16)
        .frame(width: 400)
    }

    private var header: some View {
        HStack(spacing: 12) {
            Image(systemName: StatusIcon.symbol(for: monitor.health))
                .font(.title2)
                .foregroundStyle(StatusIcon.tint(for: monitor.health))
                .frame(width: 28)
            VStack(alignment: .leading, spacing: 2) {
                Text(StatusIcon.title(for: monitor.health)).font(.headline)
                Text(subtitle).font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private var subtitle: String {
        let ip = monitor.net.interfaceIP.map { "IP \($0)" } ?? "bez IP"
        return "\(monitor.interface) · \(ip) · router \(monitor.router ?? "nieznany")"
    }

    private var targets: some View {
        let s = monitor.samples.filter { $0.link == .up }
        return Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 4) {
            GridRow {
                Text("Cel").foregroundStyle(.secondary)
                Text("Teraz").foregroundStyle(.secondary).gridColumnAlignment(.trailing)
                Text("Mediana").foregroundStyle(.secondary).gridColumnAlignment(.trailing)
                Text("Strata 5 min").foregroundStyle(.secondary).gridColumnAlignment(.trailing)
            }
            .font(.caption)
            TargetRow(name: "Router", values: s.map(\.router), color: Target.router.color)
            TargetRow(name: "NAS", values: s.map(\.nas), color: Target.nas.color)
            TargetRow(name: "Internet", values: s.map(\.internet), color: Target.internet.color)
        }
        .font(.callout.monospacedDigit())
    }

    private var wifi: some View {
        Label(monitor.wifi?.summary ?? "Wi-Fi: odczyt…", systemImage: "wifi")
            .font(.caption)
            .foregroundStyle(.secondary)
            .lineLimit(2)
    }

    private var recentEvents: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Ostatnie zdarzenia").font(.caption).foregroundStyle(.secondary)
            if monitor.events.isEmpty {
                Text("Brak").font(.caption).foregroundStyle(.tertiary)
            }
            ForEach(monitor.events.suffix(5).reversed()) { e in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(e.date, format: .dateTime.hour().minute().second())
                        .foregroundStyle(.secondary)
                        .monospacedDigit()
                    Text(e.text).lineLimit(2).fixedSize(horizontal: false, vertical: true)
                }
                .font(.caption)
            }
        }
    }

    private var footer: some View {
        HStack(spacing: 8) {
            Button("Logi", systemImage: "folder") {
                NSWorkspace.shared.open(monitor.config.logDir)
            }
            Button("Panel NAS", systemImage: "safari") {
                if let url = URL(string: monitor.config.nasURL) { NSWorkspace.shared.open(url) }
            }
            Button("Ustawienia", systemImage: "gearshape") { AppDelegate.shared?.showSettings() }
            Spacer()
            Button("Zakończ") { NSApp.terminate(nil) }
                .keyboardShortcut("q")
        }
        .controlSize(.small)
    }
}

enum Target: String, CaseIterable {
    case router = "Router", nas = "NAS", internet = "Internet"
    var color: Color {
        switch self {
        case .router: .blue
        case .nas: .green
        case .internet: .purple
        }
    }
}

private struct TargetRow: View {
    let name: String
    let values: [Double?]
    let color: Color

    var body: some View {
        let ok = values.compactMap { $0 }.sorted()
        let loss = values.isEmpty ? 0 : Double(values.count - ok.count) / Double(values.count) * 100
        GridRow {
            Label { Text(name) } icon: { Circle().fill(color).frame(width: 8, height: 8) }
            Text(values.last.map { $0.map { String(format: "%.1f ms", $0) } ?? "strata" } ?? "—")
                .foregroundStyle(values.last == .some(nil) ? .red : .primary)
            Text(ok.isEmpty ? "—" : String(format: "%.1f ms", ok[ok.count / 2]))
            Text(values.isEmpty ? "—" : String(format: "%.1f %%", loss))
                .foregroundStyle(loss > 0 ? .orange : .primary)
        }
    }
}

/// Wykres RTT z ostatnich 5 minut. Strata = czerwona kreska u góry; luka = brak pomiaru (sen, brak IP).
private struct LatencyChart: View {
    let samples: [Sample]

    private struct Point: Identifiable {
        let id: String
        let date: Date
        let ms: Double
        let target: Target
        let series: String
    }

    var body: some View {
        let now = Date()
        let start = now.addingTimeInterval(-Double(Monitor.window))
        let (points, losses, cap) = build()
        Chart {
            ForEach(points) { p in
                LineMark(x: .value("Czas", p.date), y: .value("ms", p.ms), series: .value("Seria", p.series))
                    .foregroundStyle(by: .value("Cel", p.target.rawValue))
                    .lineStyle(StrokeStyle(lineWidth: 1.2))
            }
            ForEach(losses) { p in
                PointMark(x: .value("Czas", p.date), y: .value("ms", cap))
                    .symbol(.square)
                    .symbolSize(10)
                    .foregroundStyle(.red)
            }
        }
        .chartForegroundStyleScale([
            Target.router.rawValue: Target.router.color,
            Target.nas.rawValue: Target.nas.color,
            Target.internet.rawValue: Target.internet.color,
        ])
        .chartLegend(.hidden)
        .chartXScale(domain: start...now)
        .chartYScale(domain: 0...cap)
        .chartYAxis { AxisMarks(position: .leading) }
        .chartYAxisLabel("ms", position: .topLeading)
        .chartXAxis {
            AxisMarks(values: minuteTicks(from: start, to: now)) { _ in
                AxisGridLine()
                AxisValueLabel(format: .dateTime.hour().minute())
            }
        }
        .overlay {
            if samples.isEmpty {
                Text("Czekam na pierwsze pomiary…").font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    /// Pełne minuty, bez ostatniej tuż przy prawej krawędzi (jej etykieta byłaby ucięta).
    private func minuteTicks(from start: Date, to end: Date) -> [Date] {
        var t = (start.timeIntervalSince1970 / 60).rounded(.up) * 60
        var out: [Date] = []
        while t < end.timeIntervalSince1970 - 25 { out.append(Date(timeIntervalSince1970: t)); t += 60 }
        return out
    }

    /// Linie przerywane w miejscach strat i luk (osobna seria na każdy ciągły odcinek).
    private func build() -> ([Point], [Point], Double) {
        var points: [Point] = []
        var losses: [Point] = []
        var maxMs = 0.0
        for target in Target.allCases {
            var segment = 0
            var prevEpoch: Int?
            for s in samples {
                let date = Date(timeIntervalSince1970: TimeInterval(s.epoch))
                let value: Double? = switch target {
                case .router: s.router
                case .nas: s.nas
                case .internet: s.internet
                }
                if let p = prevEpoch, s.epoch - p > 2 { segment += 1 }
                prevEpoch = s.epoch
                guard s.link != .noIP else { segment += 1; continue }
                if let v = value {
                    maxMs = max(maxMs, v)
                    points.append(Point(id: "\(target)-\(s.epoch)", date: date, ms: v, target: target,
                                        series: "\(target.rawValue)#\(segment)"))
                } else {
                    segment += 1
                    if s.link == .up {
                        losses.append(Point(id: "loss-\(target)-\(s.epoch)", date: date, ms: 0, target: target, series: ""))
                    }
                }
            }
        }
        let cap = min(200, max(20, (maxMs * 1.15).rounded(.up)))
        return (points.map { Point(id: $0.id, date: $0.date, ms: min($0.ms, cap), target: $0.target, series: $0.series) },
                losses, cap)
    }
}
