import Foundation

/// Pliki zgodne z netlog.sh: `ping-RRRR-MM-DD.csv`, `wifi-RRRR-MM-DD.csv`, `events-RRRR-MM-DD.log`.
/// Dzięki temu `summary.sh` działa bez zmian na danych z aplikacji.
public actor LogFiles {
    public static let pingHeader = "ts,link,router_ms,nas_ms,internet_ms"

    private let dir: URL
    private var handles: [String: FileHandle] = [:]
    private var handlesDay = ""

    public init(dir: URL) {
        self.dir = dir
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    public func ping(_ sample: Sample) {
        let date = Date(timeIntervalSince1970: TimeInterval(sample.epoch))
        append("ping", date: date, header: Self.pingHeader, line: "\(Stamp.local(date)),\(sample.csvFields)")
    }

    public func wifi(_ sample: WiFiSample, at date: Date) {
        append("wifi", date: date, header: WiFiSample.csvHeader, line: "\(Stamp.local(date)),\(sample.csvFields)")
    }

    public func event(_ text: String, at date: Date = Date()) {
        append("events", ext: "log", date: date, header: nil, line: "\(Stamp.local(date)) \(text)")
    }

    /// Blok zrzutu diagnostycznego, w formacie netlog.sh (`=== … ZRZUT: …`).
    public func snapshot(_ body: String, reason: String, at date: Date = Date()) {
        append("events", ext: "log", date: date, header: nil,
               line: "=== \(Stamp.local(date)) ZRZUT: \(reason)\n\(body)\n")
    }

    /// Usuwa pliki starsze niż `keepDays` (jak `find -mtime +N -delete`).
    public func cleanup(keepDays: Int) {
        let fm = FileManager.default
        guard let files = try? fm.contentsOfDirectory(at: dir, includingPropertiesForKeys: [.contentModificationDateKey]) else { return }
        let cutoff = Date().addingTimeInterval(-Double(keepDays + 1) * 86_400)
        for f in files where f.lastPathComponent.contains("-20") {
            let mod = (try? f.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate
            if let mod, mod < cutoff { try? fm.removeItem(at: f) }
        }
    }

    public func close() {
        for h in handles.values { try? h.close() }
        handles.removeAll()
    }

    private func append(_ kind: String, ext: String = "csv", date: Date, header: String?, line: String) {
        let day = Stamp.day(date)
        if day != handlesDay { close(); handlesDay = day }
        let name = "\(kind)-\(day).\(ext)"
        if handles[name] == nil {
            let url = dir.appending(path: name)
            if !FileManager.default.fileExists(atPath: url.path) {
                FileManager.default.createFile(atPath: url.path, contents: header.map { Data(($0 + "\n").utf8) })
            }
            guard let h = try? FileHandle(forWritingTo: url) else { return }
            _ = try? h.seekToEnd()
            handles[name] = h
        }
        try? handles[name]?.write(contentsOf: Data((line + "\n").utf8))
    }
}
