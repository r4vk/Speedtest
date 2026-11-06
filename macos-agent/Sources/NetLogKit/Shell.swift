import Foundation

/// Uruchamia narzędzie systemowe i zwraca jego wyjście. Tylko do zrzutów diagnostycznych,
/// AWDL i MCS — nigdy w pętli co sekundę. Blokuje, więc wołać poza wątkiem głównym.
public enum Shell {
    public static func run(_ path: String, _ args: [String], timeout: TimeInterval = 5) -> String {
        let p = Process()
        p.executableURL = URL(filePath: path)
        p.arguments = args
        let out = Pipe()
        p.standardOutput = out
        p.standardError = out
        do { try p.run() } catch { return "(\(path): \(error.localizedDescription))\n" }
        let killer = DispatchWorkItem { @Sendable in if p.isRunning { p.terminate() } }
        DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: killer)
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        killer.cancel()
        return String(decoding: data, as: UTF8.self)
    }

    /// Stan AWDL tak, jak pokazuje go `ifconfig awdl0` (`active`, `inactive`, albo `none`).
    public static func awdlStatus() -> String {
        let out = run("/sbin/ifconfig", ["awdl0"], timeout: 2)
        for line in out.split(separator: "\n") {
            let t = line.trimmingCharacters(in: .whitespaces)
            if t.hasPrefix("status:") { return t.dropFirst(7).trimmingCharacters(in: .whitespaces) }
        }
        return "none"
    }

    /// Blok „Current Network Information” z `system_profiler SPAirPortDataType`.
    public static func airportCurrentNetwork() -> String {
        let out = run("/usr/sbin/system_profiler", ["SPAirPortDataType"], timeout: 15)
        var lines: [Substring] = []
        var inside = false
        for line in out.split(separator: "\n", omittingEmptySubsequences: false) {
            if line.contains("Current Network Information:") { inside = true; continue }
            if inside && line.contains("Other Local") { break }
            if inside { lines.append(line) }
        }
        return lines.joined(separator: "\n")
    }

    /// MCS z bloku system_profiler (`MCS Index: 7`).
    public static func parseMCS(_ block: String) -> Int? {
        for line in block.split(separator: "\n") where line.contains("MCS Index:") {
            return Int(line.split(separator: ":").last?.trimmingCharacters(in: .whitespaces) ?? "")
        }
        return nil
    }
}
