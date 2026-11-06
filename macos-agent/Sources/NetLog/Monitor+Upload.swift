import Foundation
import NetLogKit

/// Stan wysyłki na NAS, do popovera i ustawień.
enum UploadState: Equatable {
    case notConfigured
    case waiting
    case ok
    case retrying(String)
    case unauthorized
    case forbidden

    var text: String {
        switch self {
        case .notConfigured: "wyłączona (brak identyfikatora lub tokenu)"
        case .waiting: "czeka na pierwszą paczkę"
        case .ok: "działa"
        case .retrying(let why): "ponowienie później: \(why)"
        case .unauthorized: "zatrzymana: NAS odrzucił token (401). Wklej nowy token w Ustawieniach."
        case .forbidden: "zatrzymana: identyfikator urządzenia nie pasuje do tokenu (403)."
        }
    }

    var isProblem: Bool {
        switch self {
        case .retrying, .unauthorized, .forbidden: true
        default: false
        }
    }
}

/// Wysyłka kolejki SQLite: paczka do 1000 wierszy co 5 minut (szybciej, gdy jest zaległość),
/// bo NAS i tak zapisuje przyjęte wiersze do bazy dopiero razem ze swoim buforem;
/// od razu po wybudzeniu, z rosnącym odstępem przy błędach. 401/403 zatrzymują wysyłkę do zmiany ustawień.
extension Monitor {
    var uploadConfigured: Bool { config.ingestDeviceID != nil && ingestToken != nil }

    /// Token: zmienna `NETLOG_INGEST_TOKEN` (testy) albo pęk kluczy. Pęku nie czytamy bez identyfikatora,
    /// żeby nie wywoływać pytania o dostęp, gdy wysyłka nie jest ustawiona.
    func loadIngestToken() {
        if let env = ProcessInfo.processInfo.environment["NETLOG_INGEST_TOKEN"], !env.isEmpty {
            ingestToken = env
        } else if config.ingestDeviceID != nil {
            ingestToken = Keychain.read()
        }
        hasIngestToken = ingestToken != nil
        resetUpload()
    }

    /// Zapisuje (lub usuwa przy `nil`) token w pęku kluczy i wznawia wysyłkę.
    func setIngestToken(_ token: String?) {
        let trimmed = token?.trimmingCharacters(in: .whitespacesAndNewlines)
        if let t = trimmed, !t.isEmpty {
            let status = Keychain.save(t)
            guard status == errSecSuccess else {
                event("Nie udało się zapisać tokenu w pęku kluczy (OSStatus \(status))", kind: "error")
                return
            }
            ingestToken = t
            event("ZMIANA ustawień: token wysyłki zapisany w pęku kluczy", kind: "config_change")
        } else {
            Keychain.delete()
            ingestToken = nil
            event("ZMIANA ustawień: token wysyłki usunięty", kind: "config_change")
        }
        hasIngestToken = ingestToken != nil
        resetUpload()
    }

    func resetUpload() {
        uploadState = uploadConfigured ? .waiting : .notConfigured
        uploadFailures = 0
        uploadBatchLimit = config.uploadBatch
        nextUploadAt = 0
    }

    /// „Wyślij teraz” — także po 401/403, gdy token poprawiono w panelu.
    func uploadNow() {
        if uploadState == .unauthorized || uploadState == .forbidden { resetUpload() }
        nextUploadAt = 0
        maybeUpload(epoch: Int(Date().timeIntervalSince1970))
    }

    func maybeUpload(epoch: Int) {
        guard !uploading, epoch >= nextUploadAt else { return }
        guard let device = config.ingestDeviceID, let token = ingestToken, let store,
              let url = Ingest.endpoint(nasURL: config.nasURL) else {
            uploadState = .notConfigured
            return
        }
        guard uploadState != .unauthorized, uploadState != .forbidden else { return }
        uploading = true
        let limit = uploadBatchLimit
        Task {
            let outcome = await Ingest.uploadBatch(store: store, endpoint: url, deviceID: device, token: token, limit: limit)
            uploading = false
            handleUpload(outcome, limit: limit)
        }
    }

    private func handleUpload(_ outcome: Ingest.Outcome, limit: Int) {
        let now = Int(Date().timeIntervalSince1970)
        switch outcome {
        case let .sent(count, accepted, duplicates, rejected, reasons, skew):
            let why = reasons.map { "\($0.key) \($0.value)" }.sorted().joined(separator: ", ")
            let note = "przyjęte \(accepted), duplikaty \(duplicates), odrzucone \(rejected)" + (why.isEmpty ? "" : " (\(why))")
            if uploadState != .ok { event("WYSYŁKA na NAS działa: \(note)", kind: "upload_ok") }
            uploadState = .ok
            uploadFailures = 0
            lastUploadAt = Date()
            lastUploadNote = note
            skewMs = skew
            uploadBatchLimit = min(config.uploadBatch, limit * 2)
            // Zaległość: kolejna paczka po 5 s, inaczej zwykły odstęp.
            nextUploadAt = now + (count >= limit ? 5 : config.uploadEvery)
            Task { unsent = await store?.unsentCount() ?? 0 }
        case .nothingToSend:
            if uploadState != .ok { uploadState = .waiting }
            nextUploadAt = now + config.uploadEvery
            unsent = 0
        case .unauthorized:
            uploadState = .unauthorized
            event("WYSYŁKA wstrzymana: NAS odrzucił token (HTTP 401). Wklej nowy token w Ustawieniach.", kind: "upload_error")
        case .forbidden:
            uploadState = .forbidden
            event("WYSYŁKA wstrzymana: identyfikator \(config.ingestDeviceID ?? "?") nie pasuje do tokenu (HTTP 403)", kind: "upload_error")
        case .tooLarge:
            uploadBatchLimit = max(50, limit / 2)
            nextUploadAt = now + 5
        case .busy(let after):
            // Bufor NAS-a pełny: nic nie przyjęto, kolejka zostaje; NAS sam mówi, kiedy wrócić.
            if !uploadState.isProblem {
                event("WYSYŁKA wstrzymana na \(after) s: bufor NAS-a pełny (HTTP 503), kolejka zostaje", kind: "upload_busy")
            }
            uploadState = .retrying("bufor NAS-a pełny, ponowienie za \(after) s")
            nextUploadAt = now + after
        case .retryLater(let why):
            uploadFailures += 1
            let delay = Ingest.backoff(failures: uploadFailures, base: config.uploadEvery)
            if uploadFailures == 1 { event("WYSYŁKA nieudana: \(why) — następna próba za \(delay) s (odstęp rośnie do 15 min), kolejka zostaje", kind: "upload_error") }
            uploadState = .retrying(why)
            nextUploadAt = now + delay
        }
    }
}
