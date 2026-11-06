import Darwin
import Foundation
import os

public enum ProbeOutcome: String, Sendable, Codable {
    case ok, timeout, error
}

/// Wynik jednej próby ICMP echo.
public struct PingReply: Sendable, Equatable {
    public var outcome: ProbeOutcome
    public var rttMs: Double?
    public var resolvedIP: String?
    public var errorKind: String?
    public var errorDetail: String?
    public var durationMs: Double

    public init(outcome: ProbeOutcome, rttMs: Double? = nil, resolvedIP: String? = nil,
                errorKind: String? = nil, errorDetail: String? = nil, durationMs: Double = 0) {
        self.outcome = outcome
        self.rttMs = rttMs
        self.resolvedIP = resolvedIP
        self.errorKind = errorKind
        self.errorDetail = errorDetail
        self.durationMs = durationMs
    }
}

/// ICMP echo przez gniazdo SOCK_DGRAM: na macOS działa bez uprawnień roota i bez procesu `ping`.
public enum ICMPPinger {
    private static let sequence = OSAllocatedUnfairLock(initialState: UInt16(0))
    private static let queue = DispatchQueue(label: "org.r4vk.netlog.icmp", qos: .utility, attributes: .concurrent)

    /// Jedna próba; blokujące czekanie odbywa się poza pulą wątków Swift Concurrency.
    public static func ping(_ host: String, timeout: TimeInterval) async -> PingReply {
        await withCheckedContinuation { cont in
            queue.async { cont.resume(returning: pingSync(host, timeout: timeout)) }
        }
    }

    public static func pingSync(_ host: String, timeout: TimeInterval) -> PingReply {
        let t0 = DispatchTime.now().uptimeNanoseconds
        func elapsed() -> Double { Double(DispatchTime.now().uptimeNanoseconds - t0) / 1e6 }
        func fail(_ kind: String, _ detail: String? = nil, ip: String? = nil) -> PingReply {
            PingReply(outcome: .error, resolvedIP: ip, errorKind: kind, errorDetail: detail, durationMs: elapsed())
        }

        guard var addr = resolveIPv4(host) else { return fail("resolve", host) }
        let ip = ipString(addr)
        let fd = socket(AF_INET, SOCK_DGRAM, IPPROTO_ICMP)
        guard fd >= 0 else { return fail("socket", String(cString: strerror(errno)), ip: ip) }
        defer { close(fd) }

        let seq = sequence.withLock { s -> UInt16 in s &+= 1; return s }
        let ident = UInt16(truncatingIfNeeded: getpid())
        let token = UInt64.random(in: 1...UInt64.max)
        let packet = echoRequest(ident: ident, seq: seq, token: token)

        let sent = packet.withUnsafeBytes { buf in
            withUnsafePointer(to: &addr) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    sendto(fd, buf.baseAddress, buf.count, 0, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
                }
            }
        }
        if sent < 0 {
            let e = errno
            let kind = (e == EHOSTUNREACH || e == ENETUNREACH || e == EHOSTDOWN || e == ENETDOWN) ? "unreachable" : "send"
            return fail(kind, String(cString: strerror(e)), ip: ip)
        }

        let deadline = t0 + UInt64(timeout * 1e9)
        var buf = [UInt8](repeating: 0, count: 1500)
        while true {
            let now = DispatchTime.now().uptimeNanoseconds
            if now >= deadline {
                return PingReply(outcome: .timeout, resolvedIP: ip, durationMs: elapsed())
            }
            var pfd = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
            let r = poll(&pfd, 1, Int32((deadline - now + 999_999) / 1_000_000))
            if r < 0 {
                if errno == EINTR { continue }
                return fail("poll", String(cString: strerror(errno)), ip: ip)
            }
            if r == 0 { continue }

            var from = sockaddr_in()
            var fromLen = socklen_t(MemoryLayout<sockaddr_in>.size)
            let n = buf.withUnsafeMutableBytes { b in
                withUnsafeMutablePointer(to: &from) {
                    $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                        recvfrom(fd, b.baseAddress, b.count, 0, $0, &fromLen)
                    }
                }
            }
            guard n > 0, from.sin_addr.s_addr == addr.sin_addr.s_addr else { continue }
            if isEchoReply(Array(buf.prefix(n)), seq: seq, token: token) {
                return PingReply(outcome: .ok, rttMs: elapsed(), resolvedIP: ip, durationMs: elapsed())
            }
        }
    }

    // MARK: - Pakiet

    package static func echoRequest(ident: UInt16, seq: UInt16, token: UInt64) -> [UInt8] {
        var p = [UInt8](repeating: 0, count: 16)
        p[0] = 8 // echo request
        p[4] = UInt8(ident >> 8); p[5] = UInt8(ident & 0xFF)
        p[6] = UInt8(seq >> 8); p[7] = UInt8(seq & 0xFF)
        withUnsafeBytes(of: token.bigEndian) { p.replaceSubrange(8..<16, with: $0) }
        let sum = checksum(p)
        p[2] = UInt8(sum >> 8); p[3] = UInt8(sum & 0xFF)
        return p
    }

    /// Odpowiedź z gniazda DGRAM może zawierać nagłówek IPv4 — pomijamy go. Identyfikator
    /// może zostać nadpisany przez jądro, więc dopasowujemy numer sekwencji i losowy token.
    package static func isEchoReply(_ data: [UInt8], seq: UInt16, token: UInt64) -> Bool {
        var off = 0
        if data.count >= 20, data[0] >> 4 == 4 { off = Int(data[0] & 0x0F) * 4 }
        guard data.count - off >= 16, data[off] == 0 else { return false }
        let rseq = UInt16(data[off + 6]) << 8 | UInt16(data[off + 7])
        guard rseq == seq else { return false }
        var rtoken: UInt64 = 0
        for i in 0..<8 { rtoken = rtoken << 8 | UInt64(data[off + 8 + i]) }
        return rtoken == token
    }

    package static func checksum(_ bytes: [UInt8]) -> UInt16 {
        var sum: UInt32 = 0
        var i = 0
        while i + 1 < bytes.count { sum += UInt32(bytes[i]) << 8 | UInt32(bytes[i + 1]); i += 2 }
        if i < bytes.count { sum += UInt32(bytes[i]) << 8 }
        while sum >> 16 != 0 { sum = (sum & 0xFFFF) + (sum >> 16) }
        return ~UInt16(sum)
    }

    // MARK: - Adresy

    static func resolveIPv4(_ host: String) -> sockaddr_in? {
        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        if inet_pton(AF_INET, host, &addr.sin_addr) == 1 { return addr }
        var hints = addrinfo()
        hints.ai_family = AF_INET
        var res: UnsafeMutablePointer<addrinfo>?
        guard getaddrinfo(host, nil, &hints, &res) == 0, let first = res else { return nil }
        defer { freeaddrinfo(res) }
        guard let sa = first.pointee.ai_addr else { return nil }
        return sa.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { $0.pointee }
    }

    static func ipString(_ addr: sockaddr_in) -> String {
        var a = addr.sin_addr
        var buf = [CChar](repeating: 0, count: Int(INET_ADDRSTRLEN))
        inet_ntop(AF_INET, &a, &buf, socklen_t(buf.count))
        return String(decoding: buf.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }, as: UTF8.self)
    }
}
