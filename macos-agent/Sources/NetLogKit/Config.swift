import Foundation

/// Ustawienia agenta. Kolejność: zmienna środowiskowa (jak w netlog.sh) > `defaults write org.r4vk.netlog` > domyślne.
public struct Config: Sendable {
    public var nasIP: String
    public var nasURL: String
    public var internetIP: String
    /// Router podany ręcznie; domyślnie `nil` = wykrywany z trasy domyślnej.
    public var routerIP: String?
    /// Katalog plików CSV i dziennika zdarzeń, wspólny z loggerem powłoki.
    public var logDir: URL
    /// Baza SQLite z wynikami do wysłania na NAS (etap 9).
    public var storePath: URL
    /// Ile dni trzymać pliki CSV i dziennik zdarzeń.
    public var keepDays: Int
    /// Ile dni trzymać wyniki w SQLite (3 próby na sekundę, więc krócej niż CSV).
    public var storeKeepDays: Int
    /// MCS nie jest dostępne w CoreWLAN; można je brać z `system_profiler` raz na minutę.
    public var mcsFromSystemProfiler: Bool

    public var pingTimeout: TimeInterval = 1     // brak odpowiedzi w tym czasie = strata
    public var stuckAfter = 3                    // tyle kolejnych strat do routera = zawieszenie
    public var snapshotEvery = 30                // zrzut w trakcie zawieszenia nie częściej niż co tyle s
    public var wifiEvery = 60                    // próbka Wi-Fi co tyle s
    public var wakeGraceMax = 30                 // po wybudzeniu najwyżej tyle s na ponowne połączenie

    public static let bundleID = "org.r4vk.netlog"
    /// Domena `defaults write org.r4vk.netlog …` — wspólna dla okna ustawień i wiersza poleceń.
    public static var defaultsStore: UserDefaults { UserDefaults(suiteName: bundleID) ?? .standard }

    /// Zmienne środowiskowe, które nadpisują ustawienia (okno ustawień o nich ostrzega).
    public static func envOverrides(_ env: [String: String] = ProcessInfo.processInfo.environment) -> [String] {
        ["NETLOG_NAS_IP", "NETLOG_NAS_URL", "NETLOG_INTERNET_IP", "NETLOG_ROUTER_IP", "NETLOG_KEEP_DAYS",
         "NETLOG_STORE_KEEP_DAYS", "NETLOG_MCS"].filter { !(env[$0] ?? "").isEmpty }
    }

    /// Zapisuje ustawienia edytowalne w oknie. Pusty router = wykrywanie automatyczne.
    public func save(to d: UserDefaults = Config.defaultsStore) {
        d.set(nasIP, forKey: "nasIP")
        d.set(nasURL, forKey: "nasURL")
        d.set(internetIP, forKey: "internetIP")
        d.set(routerIP ?? "", forKey: "routerIP")
        d.set(String(keepDays), forKey: "keepDays")
        d.set(String(storeKeepDays), forKey: "storeKeepDays")
        d.set(mcsFromSystemProfiler ? "1" : "0", forKey: "mcsFromSystemProfiler")
    }

    /// Opis różnic do dziennika zdarzeń (`ZMIANA ustawień: …`).
    public func changes(from old: Config) -> [String] {
        var out: [String] = []
        func add(_ name: String, _ a: String, _ b: String) { if a != b { out.append("\(name) \(a) -> \(b)") } }
        add("nas", old.nasIP, nasIP)
        add("panel", old.nasURL, nasURL)
        add("internet", old.internetIP, internetIP)
        add("router", old.routerIP ?? "auto", routerIP ?? "auto")
        add("csv_dni", String(old.keepDays), String(keepDays))
        add("sqlite_dni", String(old.storeKeepDays), String(storeKeepDays))
        add("mcs", old.mcsFromSystemProfiler ? "tak" : "nie", mcsFromSystemProfiler ? "tak" : "nie")
        return out
    }

    /// Sprawdza nazwę hosta lub adres IPv4 (cel pingu).
    public static func isValidHost(_ s: String) -> Bool {
        let t = s.trimmingCharacters(in: .whitespaces)
        guard !t.isEmpty, t.count <= 253, !t.contains(" ") else { return false }
        return t.allSatisfy { $0.isLetter || $0.isNumber || $0 == "." || $0 == "-" }
    }

    public static func isValidURL(_ s: String) -> Bool {
        guard let u = URL(string: s), let scheme = u.scheme?.lowercased(), u.host != nil else { return false }
        return scheme == "http" || scheme == "https"
    }

    public static func load(
        env: [String: String] = ProcessInfo.processInfo.environment,
        defaults: UserDefaults = Config.defaultsStore
    ) -> Config {
        func value(_ envKey: String, _ key: String, _ fallback: String) -> String {
            if let v = env[envKey], !v.isEmpty { return v }
            if let v = defaults.string(forKey: key), !v.isEmpty { return v }
            return fallback
        }
        let home = FileManager.default.homeDirectoryForCurrentUser
        let logDir = value("NETLOG_DIR", "logDir", home.appending(path: "Library/Logs/r4vk-netlog").path)
        let store = value("APP_STORE", "storePath",
                          home.appending(path: "Library/Application Support/NetLog/netlog.sqlite").path)
        return Config(
            nasIP: value("NETLOG_NAS_IP", "nasIP", "192.168.10.2"),
            nasURL: value("NETLOG_NAS_URL", "nasURL", "http://192.168.10.2:18000/"),
            internetIP: value("NETLOG_INTERNET_IP", "internetIP", "1.1.1.1"),
            routerIP: Optional(value("NETLOG_ROUTER_IP", "routerIP", "")).flatMap { $0.isEmpty ? nil : $0 },
            logDir: URL(filePath: logDir, directoryHint: .isDirectory),
            storePath: URL(filePath: store),
            keepDays: Int(value("NETLOG_KEEP_DAYS", "keepDays", "30")) ?? 30,
            storeKeepDays: Int(value("NETLOG_STORE_KEEP_DAYS", "storeKeepDays", "7")) ?? 7,
            mcsFromSystemProfiler: value("NETLOG_MCS", "mcsFromSystemProfiler", "1") != "0"
        )
    }
}
