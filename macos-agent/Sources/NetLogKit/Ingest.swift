import Foundation

/// Wysyłka kolejki do `POST {nasURL}api/ingest/results` (docs/network-quality-design.md §15).
public enum Ingest {
    /// Jeden wynik w formacie API. Puste pola opcjonalne nie są wysyłane.
    struct Result: Encodable {
        let external_id: String
        let target: String
        let host: String?
        let `protocol`: String
        let started_at: String
        let duration_ms: Double
        let outcome: String
        let rtt_ms: Double?
        let timeout_ms: Int
        let resolved_ip: String?
        let ip_family: Int?
        let error_kind: String?
        let error_detail: String?

        init(_ r: ProbeRecord) {
            external_id = r.externalID
            target = r.target
            host = r.host
            `protocol` = r.protocolName
            started_at = r.startedAt
            duration_ms = r.durationMs
            outcome = r.outcome.rawValue
            rtt_ms = r.outcome == .ok ? r.rttMs : nil
            timeout_ms = r.timeoutMs
            resolved_ip = r.resolvedIP
            ip_family = r.ipFamily
            error_kind = r.errorKind
            error_detail = r.errorDetail
        }
    }

    struct Payload: Encodable {
        let device_id: String
        let sent_at: String
        let results: [Result]
    }

    struct Response: Decodable {
        struct Rejected: Decodable { let external_id: String?; let reason: String? }
        let accepted: Int
        let duplicates: Int
        let rejected: [Rejected]
        let skew_ms: Double?
    }

    public enum Outcome: Sendable, Equatable {
        case sent(count: Int, accepted: Int, duplicates: Int, rejected: Int, rejectedReasons: [String: Int], skewMs: Double?)
        case nothingToSend
        /// 401: token zły lub unieważniony — wysyłka staje do zmiany ustawień.
        case unauthorized
        /// 403: `device_id` nie pasuje do tokenu.
        case forbidden
        /// 413: paczka za duża — następna będzie mniejsza.
        case tooLarge
        /// 503 z `Retry-After`: bufor NAS-a pełny, nic z paczki nie przyjęto — ponowić po tylu sekundach.
        case busy(retryAfter: Int)
        /// 5xx, brak sieci, timeout — ponowić później, kolejka zostaje.
        case retryLater(String)
    }

    /// `http://nas:18000/` → `http://nas:18000/api/ingest/results` (także bez końcowego `/`).
    public static func endpoint(nasURL: String) -> URL? {
        guard var base = URL(string: nasURL.trimmingCharacters(in: .whitespaces)), base.host != nil else { return nil }
        if !base.path.hasSuffix("/") { base = base.appending(path: "") }
        return base.appending(path: "api/ingest/results")
    }

    package static func body(deviceID: String, records: [ProbeRecord], now: Date) throws -> Data {
        let enc = JSONEncoder()
        enc.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        return try enc.encode(Payload(device_id: deviceID, sent_at: Stamp.utc(now), results: records.map(Result.init)))
    }

    /// Jedna paczka: najstarsze `limit` wierszy z kolejki, potem oznaczenie ich w bazie.
    public static func uploadBatch(store: ResultStore, endpoint: URL, deviceID: String, token: String,
                                   limit: Int, session: URLSession = .shared) async -> Outcome {
        let rows = await store.unsent(limit: limit)
        guard !rows.isEmpty else { return .nothingToSend }
        let data: Data
        do { data = try body(deviceID: deviceID, records: rows.map(\.record), now: Date()) } catch {
            return .retryLater("kodowanie JSON: \(error.localizedDescription)")
        }
        var req = URLRequest(url: endpoint, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 30)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        req.httpBody = data

        let (respData, resp): (Data, URLResponse)
        do { (respData, resp) = try await session.data(for: req) } catch {
            return .retryLater(error.localizedDescription)
        }
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        switch code {
        case 200:
            guard let r = try? JSONDecoder().decode(Response.self, from: respData) else {
                return .retryLater("niezrozumiała odpowiedź NAS-a")
            }
            var reasons: [String: String] = [:]
            var counts: [String: Int] = [:]
            for item in r.rejected {
                let reason = item.reason ?? "?"
                if let ext = item.external_id { reasons[ext] = reason }
                counts[reason, default: 0] += 1
            }
            let ids = Dictionary(uniqueKeysWithValues: rows.map { ($0.rowID, $0.record.externalID) })
            await store.markUploaded(rowIDs: rows.map(\.rowID), rejected: reasons, externalIDs: ids)
            return .sent(count: rows.count, accepted: r.accepted, duplicates: r.duplicates, rejected: r.rejected.count,
                         rejectedReasons: counts, skewMs: r.skew_ms)
        case 401: return .unauthorized
        case 403: return .forbidden
        case 413: return .tooLarge
        case 404: return .retryLater("NAS nie ma jeszcze API wysyłki (HTTP 404) — potrzebna nowsza wersja Speedtest")
        case 503:
            let header = (resp as? HTTPURLResponse)?.value(forHTTPHeaderField: "Retry-After")
            if let s = retryAfter(header) { return .busy(retryAfter: s) }
            return .retryLater("bufor zapisu na NAS-ie pełny (HTTP 503)")
        default: return .retryLater("HTTP \(code)")
        }
    }

    /// `Retry-After` w sekundach (NAS nie używa formy z datą HTTP), w granicach 1 s – 1 h.
    public static func retryAfter(_ header: String?) -> Int? {
        guard let v = header?.trimmingCharacters(in: .whitespaces), let n = Int(v), n >= 0 else { return nil }
        return min(3600, max(1, n))
    }

    /// Opóźnienie po kolejnych nieudanych próbach: 60 s, 2, 4, 8 min… najwyżej 15 min.
    public static func backoff(failures: Int, base: Int = 60) -> Int {
        min(900, base << min(max(failures - 1, 0), 4))
    }

    /// Identyfikator urządzenia z panelu Speedtest: małe litery, cyfry i „-”, nie „nas”.
    public static func isValidDeviceID(_ s: String) -> Bool {
        guard s != "nas", (1...40).contains(s.count), let first = s.first, first.isLetter || first.isNumber else { return false }
        return s.allSatisfy { ($0.isASCII && ($0.isLowercase || $0.isNumber)) || $0 == "-" }
    }
}
