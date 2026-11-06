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

    public static func load(
        env: [String: String] = ProcessInfo.processInfo.environment,
        defaults: UserDefaults = UserDefaults(suiteName: bundleID) ?? .standard
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
