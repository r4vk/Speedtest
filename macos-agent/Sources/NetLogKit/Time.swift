import Foundation

/// Formaty czasu. CSV i dziennik zdarzeń: czas lokalny jak w netlog.sh (`2026-11-06T15:03:35+0100`).
/// SQLite i API NAS-a: UTC z milisekundami i `Z` (jak `to_iso_z` po stronie serwera).
public enum Stamp {
    public static func local(_ date: Date) -> String { strftime(date, "%Y-%m-%dT%H:%M:%S%z") }
    public static func day(_ date: Date) -> String { strftime(date, "%Y-%m-%d") }

    public static func utc(_ date: Date) -> String {
        // ISO8601FormatStyle obcina ułamek (…35.123 → .122), więc milisekundy dopisujemy sami, zaokrąglone.
        let ms = Int64((date.timeIntervalSince1970 * 1000).rounded())
        let whole = Date(timeIntervalSince1970: TimeInterval(ms.quotientAndRemainder(dividingBy: 1000).quotient))
        let base = Date.ISO8601FormatStyle().format(whole) // 2026-11-06T14:03:35Z
        return String(base.dropLast()) + String(format: ".%03dZ", Int(ms % 1000))
    }

    private static func strftime(_ date: Date, _ format: String) -> String {
        var t = time_t(date.timeIntervalSince1970.rounded(.down))
        var tm = tm()
        localtime_r(&t, &tm)
        var buf = [CChar](repeating: 0, count: 64)
        let n = Foundation.strftime(&buf, buf.count, format, &tm)
        return String(decoding: buf.prefix(n).map { UInt8(bitPattern: $0) }, as: UTF8.self)
    }
}
