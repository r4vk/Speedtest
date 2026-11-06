import Foundation
import SQLite3

/// Jedna próba w kształcie `ProbeResult` z serwera (docs/network-quality-design.md §15).
/// `externalID` jest deterministyczny (sekunda + cel), więc ponowna wysyłka jest idempotentna
/// dzięki unikalnemu indeksowi `(device_id, external_id)` po stronie NAS-a.
public struct ProbeRecord: Sendable, Equatable, Codable {
    public var externalID: String
    public var target: String          // nazwa celu: "gateway", "nas", "internet" — dopasowanie po nazwie
    public var host: String?
    public var protocolName = "icmp"
    public var startedAt: String       // UTC, ms, Z
    public var durationMs: Double
    public var outcome: ProbeOutcome
    public var rttMs: Double?
    public var timeoutMs: Int
    public var resolvedIP: String?
    public var ipFamily: Int?
    public var errorKind: String?
    public var errorDetail: String?
    public var link: LinkState

    public init(epochMs: Int64, target: String, host: String?, reply: PingReply, timeoutMs: Int, link: LinkState) {
        self.externalID = "\(epochMs):\(target)"
        self.target = target
        self.host = host
        self.startedAt = Stamp.utc(Date(timeIntervalSince1970: Double(epochMs) / 1000))
        self.durationMs = (reply.durationMs * 1000).rounded() / 1000
        self.outcome = reply.outcome
        self.rttMs = reply.rttMs.map { ($0 * 1000).rounded() / 1000 }
        self.timeoutMs = timeoutMs
        self.resolvedIP = reply.resolvedIP
        self.ipFamily = reply.resolvedIP == nil ? nil : 4
        self.errorKind = reply.errorKind
        self.errorDetail = reply.errorDetail
        self.link = link
    }
}

public enum StoreError: Error, CustomStringConvertible {
    case sqlite(String)
    public var description: String { switch self { case .sqlite(let m): "SQLite: \(m)" } }
}

/// Lokalny bufor wyników. Jedno otwarte połączenie, WAL, zapis partiami (co kilka sekund),
/// żeby nie robić checkpointu przy każdej próbie.
public actor ResultStore {
    private var db: OpaquePointer?
    private var pending: [ProbeRecord] = []
    public let deviceID: String
    public static let schemaVersion: Int32 = 1

    public init(path: URL) throws {
        try? FileManager.default.createDirectory(at: path.deletingLastPathComponent(), withIntermediateDirectories: true)
        var handle: OpaquePointer?
        guard sqlite3_open_v2(path.path, &handle, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX, nil) == SQLITE_OK else {
            let msg = handle.map { String(cString: sqlite3_errmsg($0)) } ?? "open"
            sqlite3_close(handle)
            throw StoreError.sqlite(msg)
        }
        try Self.exec(handle, """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS probe_results (
              id INTEGER PRIMARY KEY,
              external_id TEXT NOT NULL UNIQUE,
              target TEXT NOT NULL,
              host TEXT,
              protocol TEXT NOT NULL DEFAULT 'icmp',
              started_at TEXT NOT NULL,
              duration_ms REAL NOT NULL,
              outcome TEXT NOT NULL CHECK (outcome IN ('ok','timeout','error')),
              rtt_ms REAL,
              timeout_ms INTEGER NOT NULL,
              resolved_ip TEXT,
              ip_family INTEGER,
              error_kind TEXT,
              error_detail TEXT,
              link TEXT NOT NULL,
              uploaded_at TEXT
            );
            CREATE INDEX IF NOT EXISTS probe_results_unsent ON probe_results(id) WHERE uploaded_at IS NULL;
            CREATE INDEX IF NOT EXISTS probe_results_started ON probe_results(started_at);
            CREATE TABLE IF NOT EXISTS wifi_samples (
              id INTEGER PRIMARY KEY, ts TEXT NOT NULL, interface TEXT, rssi_dbm INTEGER, noise_dbm INTEGER,
              channel INTEGER, band TEXT, width TEXT, tx_rate_mbps REAL, mcs INTEGER, phy TEXT, awdl TEXT,
              uploaded_at TEXT
            );
            -- Zdarzenia: start, uśpienie, wybudzenie, zawieszenia, przerwy. Okresy snu są tu jawnie,
            -- żeby serwer nie uznał ich za brak łączności.
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT, uploaded_at TEXT
            );
            PRAGMA user_version=\(Self.schemaVersion);
            """)
        db = handle
        deviceID = try Self.loadOrCreateDeviceID(handle)
    }

    isolated deinit { sqlite3_close(db) }

    // MARK: - Zapis

    public func add(_ records: [ProbeRecord]) { pending.append(contentsOf: records) }

    /// Zapisuje bufor w jednej transakcji. Wołane co kilka sekund, przed uśpieniem i przy wyjściu.
    @discardableResult
    public func flush() -> Int {
        guard !pending.isEmpty, let db else { return 0 }
        let sql = """
            INSERT OR IGNORE INTO probe_results
            (external_id,target,host,protocol,started_at,duration_ms,outcome,rtt_ms,timeout_ms,resolved_ip,ip_family,error_kind,error_detail,link)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return 0 }
        defer { sqlite3_finalize(stmt) }
        sqlite3_exec(db, "BEGIN", nil, nil, nil)
        for r in pending {
            bind(stmt, 1, r.externalID); bind(stmt, 2, r.target); bind(stmt, 3, r.host)
            bind(stmt, 4, r.protocolName); bind(stmt, 5, r.startedAt); bind(stmt, 6, r.durationMs)
            bind(stmt, 7, r.outcome.rawValue); bind(stmt, 8, r.rttMs); bind(stmt, 9, r.timeoutMs)
            bind(stmt, 10, r.resolvedIP); bind(stmt, 11, r.ipFamily); bind(stmt, 12, r.errorKind)
            bind(stmt, 13, r.errorDetail); bind(stmt, 14, r.link.rawValue)
            sqlite3_step(stmt)
            sqlite3_reset(stmt)
        }
        sqlite3_exec(db, "COMMIT", nil, nil, nil)
        let n = pending.count
        pending.removeAll(keepingCapacity: true)
        return n
    }

    public func addWiFi(_ s: WiFiSample, at date: Date) {
        run("INSERT INTO wifi_samples (ts,interface,rssi_dbm,noise_dbm,channel,band,width,tx_rate_mbps,mcs,phy,awdl) VALUES (?,?,?,?,?,?,?,?,?,?,?)") { st in
            bind(st, 1, Stamp.utc(date)); bind(st, 2, s.interface); bind(st, 3, s.rssi); bind(st, 4, s.noise)
            bind(st, 5, s.channel); bind(st, 6, s.band); bind(st, 7, s.width); bind(st, 8, s.txRate)
            bind(st, 9, s.mcs); bind(st, 10, s.phy); bind(st, 11, s.awdl)
        }
    }

    public func addEvent(kind: String, detail: String?, at date: Date = Date()) {
        run("INSERT INTO events (ts,kind,detail) VALUES (?,?,?)") { st in
            bind(st, 1, Stamp.utc(date)); bind(st, 2, kind); bind(st, 3, detail)
        }
    }

    /// Usuwa wyniki starsze niż `days` dni (wysłane czy nie — CSV trzyma dłuższą historię).
    public func prune(olderThanDays days: Int, now: Date = Date()) {
        let cutoff = Stamp.utc(now.addingTimeInterval(-Double(days) * 86_400))
        for table in ["probe_results": "started_at", "wifi_samples": "ts", "events": "ts"] {
            run("DELETE FROM \(table.key) WHERE \(table.value) < ?") { bind($0, 1, cutoff) }
        }
    }

    // MARK: - Kolejka do wysyłki (etap 9)

    /// Najstarsze niewysłane próby, do zbudowania `{"device_id","sent_at","results":[…]}`.
    public func unsent(limit: Int = 500) -> [(rowID: Int64, record: ProbeRecord)] {
        flush()
        var out: [(Int64, ProbeRecord)] = []
        query("SELECT id,external_id,target,host,protocol,started_at,duration_ms,outcome,rtt_ms,timeout_ms,resolved_ip,ip_family,error_kind,error_detail,link FROM probe_results WHERE uploaded_at IS NULL ORDER BY id LIMIT ?",
              bindings: { bind($0, 1, limit) }) { st in
            var r = ProbeRecord(epochMs: 0, target: "", host: nil, reply: PingReply(outcome: .ok), timeoutMs: 0, link: .up)
            r.externalID = text(st, 1) ?? ""
            r.target = text(st, 2) ?? ""
            r.host = text(st, 3)
            r.protocolName = text(st, 4) ?? "icmp"
            r.startedAt = text(st, 5) ?? ""
            r.durationMs = sqlite3_column_double(st, 6)
            r.outcome = ProbeOutcome(rawValue: text(st, 7) ?? "") ?? .error
            r.rttMs = sqlite3_column_type(st, 8) == SQLITE_NULL ? nil : sqlite3_column_double(st, 8)
            r.timeoutMs = Int(sqlite3_column_int64(st, 9))
            r.resolvedIP = text(st, 10)
            r.ipFamily = sqlite3_column_type(st, 11) == SQLITE_NULL ? nil : Int(sqlite3_column_int64(st, 11))
            r.errorKind = text(st, 12)
            r.errorDetail = text(st, 13)
            r.link = LinkState(rawValue: text(st, 14) ?? "") ?? .up
            out.append((sqlite3_column_int64(st, 0), r))
        }
        return out
    }

    /// Oznacza próby jako przyjęte przez NAS (także `duplicates` z odpowiedzi API).
    public func markUploaded(rowIDs: [Int64], at date: Date = Date()) {
        guard let db, !rowIDs.isEmpty else { return }
        sqlite3_exec(db, "BEGIN", nil, nil, nil)
        for id in rowIDs {
            run("UPDATE probe_results SET uploaded_at=? WHERE id=?") { bind($0, 1, Stamp.utc(date)); bind($0, 2, id) }
        }
        sqlite3_exec(db, "COMMIT", nil, nil, nil)
    }

    public func unsentCount() -> Int {
        flush()
        var n = 0
        query("SELECT COUNT(*) FROM probe_results WHERE uploaded_at IS NULL", bindings: { _ in }) { n = Int(sqlite3_column_int64($0, 0)) }
        return n
    }

    // MARK: - SQLite

    private func run(_ sql: String, _ bindings: (OpaquePointer?) -> Void) {
        guard let db else { return }
        var st: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &st, nil) == SQLITE_OK else { return }
        bindings(st)
        sqlite3_step(st)
        sqlite3_finalize(st)
    }

    private func query(_ sql: String, bindings: (OpaquePointer?) -> Void, row: (OpaquePointer?) -> Void) {
        guard let db else { return }
        var st: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &st, nil) == SQLITE_OK else { return }
        bindings(st)
        while sqlite3_step(st) == SQLITE_ROW { row(st) }
        sqlite3_finalize(st)
    }

    private static func exec(_ db: OpaquePointer?, _ sql: String) throws {
        var err: UnsafeMutablePointer<CChar>?
        if sqlite3_exec(db, sql, nil, nil, &err) != SQLITE_OK {
            let msg = err.map { String(cString: $0) } ?? "exec"
            sqlite3_free(err)
            throw StoreError.sqlite(msg)
        }
    }

    /// Lokalny identyfikator Maca. Przy rejestracji w `POST /api/devices` NAS nada własny `device_id`
    /// i token — ten identyfikator tylko odróżnia instalacje.
    private static func loadOrCreateDeviceID(_ db: OpaquePointer?) throws -> String {
        var st: OpaquePointer?
        sqlite3_prepare_v2(db, "SELECT value FROM meta WHERE key='local_device_id'", -1, &st, nil)
        defer { sqlite3_finalize(st) }
        if sqlite3_step(st) == SQLITE_ROW, let v = text(st, 0) { return v }
        let id = "mac-" + UUID().uuidString.lowercased()
        try exec(db, "INSERT INTO meta(key,value) VALUES ('local_device_id','\(id)')")
        return id
    }
}

// MARK: - Bindowanie

private let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

private func bind(_ st: OpaquePointer?, _ i: Int32, _ v: String?) {
    if let v { sqlite3_bind_text(st, i, v, -1, transient) } else { sqlite3_bind_null(st, i) }
}
private func bind(_ st: OpaquePointer?, _ i: Int32, _ v: Double?) {
    if let v { sqlite3_bind_double(st, i, v) } else { sqlite3_bind_null(st, i) }
}
private func bind(_ st: OpaquePointer?, _ i: Int32, _ v: Int?) {
    if let v { sqlite3_bind_int64(st, i, Int64(v)) } else { sqlite3_bind_null(st, i) }
}
private func bind(_ st: OpaquePointer?, _ i: Int32, _ v: Int64) { sqlite3_bind_int64(st, i, v) }
private func text(_ st: OpaquePointer?, _ i: Int32) -> String? {
    guard let c = sqlite3_column_text(st, i) else { return nil }
    return String(cString: c)
}
