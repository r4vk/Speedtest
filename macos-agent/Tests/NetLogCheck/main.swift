// Sprawdzenia logiki NetLogKit. Pełne Xcode nie jest wymagane (Swift Testing i XCTest
// potrzebują wtyczek z Xcode), więc to zwykły program: `swift run NetLogCheck`, kod wyjścia 0 = OK.
import Foundation
import NetLogKit

enum StuckDetectorTests {
    static func opensAfterThreeLossesLikeShellLogger() {
        var d = StuckDetector(stuckAfter: 3, snapshotEvery: 30)
        expect(d.feed(epoch: 100, routerOK: false, nasOK: false, internetOK: false).isEmpty)
        expect(d.feed(epoch: 101, routerOK: false, nasOK: false, internetOK: false).isEmpty)
        let out = d.feed(epoch: 102, routerOK: false, nasOK: true, internetOK: false)
        expect(out == [.started(lossRun: 3, nasOK: true, internetOK: false), .snapshot(lossRun: 3)])
        expect(d.incidentStart == 100)
        expect(d.feed(epoch: 103, routerOK: false, nasOK: false, internetOK: false).isEmpty)
        expect(d.feed(epoch: 105, routerOK: true, nasOK: true, internetOK: true) == [.ended(durationS: 5, lost: 4)])
        expect(!d.inIncident)
    }

    static func snapshotsAreThrottled() {
        var d = StuckDetector(stuckAfter: 1, snapshotEvery: 30)
        var snaps = 0
        for t in 0..<65 {
            snaps += d.feed(epoch: t, routerOK: false, nasOK: false, internetOK: false).filter { if case .snapshot = $0 { true } else { false } }.count
        }
        expect(snaps == 3) // 0, 30, 60
    }

    static func sleepInterruptsIncidentWithoutCountingSleep() {
        var d = StuckDetector()
        for t in 0..<5 { _ = d.feed(epoch: t, routerOK: false, nasOK: false, internetOK: false) }
        expect(d.pause(epoch: 5) == [.interrupted(durationS: 5, lost: 5)])
        // Po wybudzeniu pierwsza strata nie wznawia starego zawieszenia.
        expect(d.feed(epoch: 3600, routerOK: false, nasOK: false, internetOK: false).isEmpty)
        expect(!d.inIncident)
    }

    static func graceResetsLossRun() {
        var d = StuckDetector()
        _ = d.feed(epoch: 0, routerOK: false, nasOK: false, internetOK: false)
        _ = d.feed(epoch: 1, routerOK: false, nasOK: false, internetOK: false)
        d.grace()
        expect(d.feed(epoch: 2, routerOK: false, nasOK: false, internetOK: false).isEmpty)
    }
}

enum HealthTests {
    static func ok(_ e: Int) -> Sample { Sample(epoch: e, link: .up, router: 3, nas: 4, internet: 9) }

    static func states() {
        expect(Health.evaluate(recent: [], inIncident: false) == .starting)
        expect(Health.evaluate(recent: (0..<10).map { ok($0) }, inIncident: false) == .ok)
        expect(Health.evaluate(recent: (0..<10).map { ok($0) }, inIncident: true) == .stuck)
        var s = (0..<10).map { ok($0) }; s[5].router = nil
        expect(Health.evaluate(recent: s, inIncident: false) == .lag)
        s = (0..<10).map { ok($0) }; s[3].internet = nil
        expect(Health.evaluate(recent: s, inIncident: false) == .ok) // jedna strata do internetu to jeszcze nie problem
        s[4].internet = 300; s[6].internet = 300
        expect(Health.evaluate(recent: s, inIncident: false) == .lag)
        expect(Health.evaluate(recent: [ok(0), Sample(epoch: 1, link: .noIP)], inIncident: false) == .noIP)
        expect(Health.evaluate(recent: [ok(0), Sample(epoch: 1, link: .wake)], inIncident: false) == .waking)
    }
}

enum ICMPTests {
    static func packetAndReplyParsing() {
        let req = ICMPPinger.echoRequest(ident: 0x1234, seq: 7, token: 0xDEAD_BEEF_0102_0304)
        expect(req.count == 16)
        expect(ICMPPinger.checksum(req) == 0) // poprawna suma kontrolna sumuje się do zera
        var reply = req
        reply[0] = 0
        expect(ICMPPinger.isEchoReply(reply, seq: 7, token: 0xDEAD_BEEF_0102_0304))
        expect(!ICMPPinger.isEchoReply(reply, seq: 8, token: 0xDEAD_BEEF_0102_0304))
        expect(!ICMPPinger.isEchoReply(reply, seq: 7, token: 1))
        let withIPHeader = [UInt8(0x45)] + [UInt8](repeating: 0, count: 19) + reply
        expect(ICMPPinger.isEchoReply(withIPHeader, seq: 7, token: 0xDEAD_BEEF_0102_0304))
    }

    static func pingsLoopback() async {
        let r = await ICMPPinger.ping("127.0.0.1", timeout: 1)
        expect(r.outcome == .ok)
        expect((r.rttMs ?? 99) < 50)
    }

    static func unresolvableHostIsError() async {
        let r = await ICMPPinger.ping("nie-ma-takiego-hosta.invalid", timeout: 1)
        expect(r.outcome == .error)
        expect(r.errorKind == "resolve")
    }
}

enum FormatTests {
    static func pingRowMatchesShellLogger() {
        expect(Sample(epoch: 0, link: .up, router: 4.4621, nas: nil, internet: 7.8).csvFields == "up,4.462,,7.800")
        expect(Sample(epoch: 0, link: .noIP).csvFields == "no_ip,,,")
    }

    static func wifiRowMatchesShellLogger() {
        let w = WiFiSample(powerOn: true, rssi: -60, noise: -93, channel: 44, band: "5GHz", width: "80MHz",
                           txRate: 720, mcs: 7, phy: "802.11ax", awdl: "active")
        expect(w.csvFields == "-60,-93,44,5GHz,80MHz,720,7,802.11ax,active")
    }

    static func stamps() {
        let d = Date(timeIntervalSince1970: 1_762_437_815.123)
        expect(Stamp.utc(d) == "2025-11-06T14:03:35.123Z")
        expect(Stamp.local(d).count == "2025-11-06T15:03:35+0100".count)
    }

    static func mcsParsing() {
        expect(Shell.parseMCS("          PHY Mode: 802.11ax\n          MCS Index: 7\n") == 7)
        expect(Shell.parseMCS("nic") == nil)
    }
}

enum ResultStoreTests {
    static func queueRoundTripIsIdempotent() async throws {
        let path = FileManager.default.temporaryDirectory.appending(path: "netlog-test-\(UUID().uuidString).sqlite")
        defer { try? FileManager.default.removeItem(at: path) }
        let store = try ResultStore(path: path)
        let rec = ProbeRecord(epochMs: 1_762_437_815_000, target: "gateway", host: "192.168.10.1",
                              reply: PingReply(outcome: .ok, rttMs: 4.4621, resolvedIP: "192.168.10.1", durationMs: 4.5),
                              timeoutMs: 1000, link: .up)
        await store.add([rec, rec]) // ta sama sekunda i cel = ten sam external_id
        await store.add([ProbeRecord(epochMs: 1_762_437_815_000, target: "internet", host: "1.1.1.1",
                                     reply: PingReply(outcome: .timeout), timeoutMs: 1000, link: .up)])
        expect(await store.unsentCount() == 2)
        let rows = await store.unsent()
        expect(rows.first?.record.externalID == "1762437815000:gateway")
        expect(rows.first?.record.startedAt == "2025-11-06T14:03:35.000Z")
        expect(rows.first?.record.rttMs == 4.462)
        expect(rows.last?.record.outcome == .timeout)
        await store.markUploaded(rowIDs: [rows[0].rowID])
        expect(await store.unsentCount() == 1)
        let id1 = await store.deviceID
        let again = try ResultStore(path: path)
        expect(await again.deviceID == id1)
    }
}

enum ConfigTests {
    static func validationAndChanges() {
        expect(Config.isValidHost("192.168.10.2"))
        expect(Config.isValidHost("nas.local"))
        expect(!Config.isValidHost(""))
        expect(!Config.isValidHost("192.168 .10.2"))
        expect(Config.isValidURL("http://192.168.10.2:18000/"))
        expect(!Config.isValidURL("192.168.10.2"))
        expect(!Config.isValidURL("ftp://nas/"))

        let suite = "org.r4vk.netlog.check-\(UUID().uuidString)"
        let d = UserDefaults(suiteName: suite)!
        defer { d.removePersistentDomain(forName: suite) }
        let old = Config.load(env: [:], defaults: d)
        var new = old
        new.nasIP = "192.168.10.3"
        new.routerIP = "192.168.10.254"
        new.keepDays = 14
        expect(new.changes(from: old) == ["nas 192.168.10.2 -> 192.168.10.3", "router auto -> 192.168.10.254", "csv_dni 30 -> 14"])
        new.save(to: d)
        let back = Config.load(env: [:], defaults: d)
        expect(back.nasIP == "192.168.10.3" && back.routerIP == "192.168.10.254" && back.keepDays == 14)
        expect(back.changes(from: new).isEmpty)
        var auto = back
        auto.routerIP = nil
        auto.save(to: d)
        expect(Config.load(env: [:], defaults: d).routerIP == nil)
        // Zmienna środowiskowa wygrywa z zapisanym ustawieniem.
        expect(Config.load(env: ["NETLOG_NAS_IP": "10.0.0.9"], defaults: d).nasIP == "10.0.0.9")
    }
}

nonisolated(unsafe) var failures = 0 // program jednowątkowy, wołany sekwencyjnie
func expect(_ ok: Bool, file: String = #fileID, line: Int = #line) {
    if !ok { failures += 1; print("FAIL \(file):\(line)") }
}

StuckDetectorTests.opensAfterThreeLossesLikeShellLogger()
StuckDetectorTests.snapshotsAreThrottled()
StuckDetectorTests.sleepInterruptsIncidentWithoutCountingSleep()
StuckDetectorTests.graceResetsLossRun()
HealthTests.states()
ICMPTests.packetAndReplyParsing()
await ICMPTests.pingsLoopback()
await ICMPTests.unresolvableHostIsError()
FormatTests.pingRowMatchesShellLogger()
FormatTests.wifiRowMatchesShellLogger()
FormatTests.stamps()
FormatTests.mcsParsing()
try await ResultStoreTests.queueRoundTripIsIdempotent()
ConfigTests.validationAndChanges()
print(failures == 0 ? "PASS NetLogCheck" : "FAIL NetLogCheck: \(failures)")
exit(failures == 0 ? 0 : 1)
