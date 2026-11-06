import AppKit
import NetLogKit
import Observation
import ServiceManagement

/// Wpis zdarzenia do pokazania w popoverze.
struct EventLine: Identifiable {
    let id = UUID()
    let date: Date
    let text: String
}

/// Pętla pomiarowa: co sekundę ICMP do routera, NAS-a i internetu; Wi-Fi co minutę;
/// zawieszenia ze zrzutem; uśpienie i wybudzenie z NSWorkspace, żeby sen nie był liczony jako awaria.
@Observable
final class Monitor {
    static let shared = Monitor()
    static let window = 300 // sekund historii w popoverze

    let config = Config.load()
    private(set) var samples: [Sample] = []
    private(set) var health: Health = .starting
    private(set) var wifi: WiFiSample?
    private(set) var net = NetState()
    private(set) var router: String?
    private(set) var interface = "en0"
    private(set) var events: [EventLine] = []
    private(set) var legacyLoggerRunning = false
    private(set) var unsent = 0
    private(set) var storeError: String?
    /// NAS nieosiągalny („No route to host”), choć router odpowiada: prawie zawsze brak zgody na sieć lokalną.
    private(set) var localNetworkBlocked = false
    private(set) var launchAtLogin = SMAppService.mainApp.status == .enabled

    @ObservationIgnored private var loop: Task<Void, Never>?
    @ObservationIgnored private var detector: StuckDetector
    @ObservationIgnored private let files: LogFiles
    @ObservationIgnored private var store: ResultStore?
    @ObservationIgnored private var started = false
    @ObservationIgnored private var sleeping = false
    @ObservationIgnored private var wokeAt: Int?
    @ObservationIgnored private var graceUntil: Int?
    @ObservationIgnored private var lastTickEpoch: Int?
    @ObservationIgnored private var lastLink: LinkState?
    @ObservationIgnored private var lastDay = ""
    @ObservationIgnored private var lastWiFi = 0
    @ObservationIgnored private var lastWiFiDisplay = 0
    @ObservationIgnored private var lastFlush = 0
    @ObservationIgnored private var lastCount = 0
    @ObservationIgnored private var observers: [NSObjectProtocol] = []

    private init() {
        detector = StuckDetector(stuckAfter: config.stuckAfter, snapshotEvery: config.snapshotEvery)
        files = LogFiles(dir: config.logDir)
    }

    // MARK: - Start i stop

    func start() {
        guard !started else { return }
        started = true
        interface = WiFiReader.interfaceName()
        net = NetInfo.current(interface: interface)
        router = config.routerIP ?? net.router
        do { store = try ResultStore(path: config.storePath) } catch { storeError = "\(error)" }

        let version = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "dev"
        event("START agent NetLog \(version), router=\(router ?? "?") nas=\(config.nasIP) internet=\(config.internetIP) iface=\(interface)",
              kind: "start")

        let ws = NSWorkspace.shared.notificationCenter
        observers.append(ws.addObserver(forName: NSWorkspace.willSleepNotification, object: nil, queue: .main) { _ in
            MainActor.assumeIsolated { Monitor.shared.willSleep() }
        })
        observers.append(ws.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { _ in
            MainActor.assumeIsolated { Monitor.shared.didWake() }
        })

        Task { legacyLoggerRunning = await WiFiReader.legacyLoggerRunning() }
        startLoop()
        simulateSleepIfAsked()
    }

    /// Wyjście z aplikacji. Na wątku głównym tylko zatrzymuje pętlę i układa ostatnie zdarzenia;
    /// zapis robi zwrócona funkcja poza wątkiem głównym (AppKit czeka wtedy w zagnieżdżonej pętli,
    /// w której zadania głównego aktora mogą nie ruszyć).
    func prepareStop() -> @Sendable () async -> Void {
        guard started else { return {} }
        loop?.cancel()
        loop = nil
        let now = Date()
        var lines: [(String, String)] = []
        for case let .interrupted(d, lost) in detector.pause(epoch: Int(now.timeIntervalSince1970)) {
            lines.append((Self.interruptedText(d, lost), "stuck_interrupted"))
        }
        lines.append(("STOP agent NetLog (pomiary zatrzymane, to nie jest awaria sieci)", "stop"))
        let files = files, store = store, final = lines
        return {
            for (text, kind) in final {
                await files.event(text, at: now)
                await store?.addEvent(kind: kind, detail: text, at: now)
            }
            await store?.flush()
            await files.close()
        }
    }

    nonisolated static func interruptedText(_ d: Int, _ lost: Int) -> String {
        "KONIEC zawieszenia (przerwane: uśpienie, przerwa w pomiarach lub zatrzymanie agenta): trwało co najmniej \(d) s, utracone pingi do routera: \(lost)"
    }

    private func startLoop() {
        loop = Task {
            let clock = ContinuousClock()
            var next = clock.now
            while !Task.isCancelled {
                await tick()
                next = next.advanced(by: .seconds(1))
                if next < clock.now { next = clock.now } // po przeciągnięciu nie nadrabiamy zaległych sekund
                try? await Task.sleep(until: next, clock: clock)
            }
        }
    }

    /// Start przy logowaniu przez SMAppService (zastępuje LaunchAgent loggera powłoki).
    func setLaunchAtLogin(_ on: Bool) {
        do {
            if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
        } catch {
            event("Nie udało się zmienić startu przy logowaniu: \(error.localizedDescription)", kind: "error")
        }
        launchAtLogin = SMAppService.mainApp.status == .enabled
    }

    // MARK: - Uśpienie

    func willSleep() {
        guard !sleeping else { return }
        sleeping = true
        loop?.cancel()
        loop = nil
        handle(detector.pause(epoch: Int(Date().timeIntervalSince1970)))
        event("USYPIANIE: pomiary wstrzymane na czas snu (to nie jest awaria sieci)", kind: "sleep")
        health = .sleeping
        Task { await store?.flush() }
    }

    func didWake() {
        guard sleeping else { return }
        sleeping = false
        let now = Int(Date().timeIntervalSince1970)
        wokeAt = now
        graceUntil = now + config.wakeGraceMax
        lastTickEpoch = nil
        event("WYBUDZENIE: pomiary wznowione, do \(config.wakeGraceMax) s na ponowne połączenie Wi-Fi", kind: "wake")
        startLoop()
    }

    /// `APP_SIMULATE_SLEEP=5,20`: po 5 s wysyła sobie „uśpienie”, po kolejnych 20 s „wybudzenie”.
    /// Te same powiadomienia NSWorkspace co prawdziwy sen — do sprawdzenia bez usypiania Maca.
    private func simulateSleepIfAsked() {
        guard let spec = ProcessInfo.processInfo.environment["APP_SIMULATE_SLEEP"] else { return }
        let parts = spec.split(separator: ",").compactMap { Double($0) }
        guard parts.count == 2 else { return }
        Task {
            try? await Task.sleep(for: .seconds(parts[0]))
            NSWorkspace.shared.notificationCenter.post(name: NSWorkspace.willSleepNotification, object: NSWorkspace.shared)
            try? await Task.sleep(for: .seconds(parts[1]))
            NSWorkspace.shared.notificationCenter.post(name: NSWorkspace.didWakeNotification, object: NSWorkspace.shared)
        }
    }

    // MARK: - Jedna sekunda

    private func tick() async {
        let now = Date()
        let epoch = Int(now.timeIntervalSince1970)
        dailyCleanup(now)

        // Przerwa bez powiadomienia o uśpieniu (zawieszony proces, ciemne wybudzenie): jawnie, nie jako strata.
        if let last = lastTickEpoch, epoch - last > 5 {
            handle(detector.pause(epoch: last))
            event("PRZERWA w pomiarach: \(epoch - last) s bez powiadomienia o uśpieniu (to nie jest strata pakietów)", kind: "gap")
            wokeAt = epoch
            graceUntil = epoch + config.wakeGraceMax
        }
        lastTickEpoch = epoch

        let state = NetInfo.current(interface: interface)
        if config.routerIP == nil, let new = state.router, new != router {
            if router != nil { event("ZMIANA routera: \(router ?? "brak") -> \(new)", kind: "router_change") }
            router = new
        }
        net = state

        let sample: Sample
        if state.interfaceIP == nil {
            if lastLink != .noIP { event("BRAK IP na \(interface) (Wi-Fi wyłączone lub roaming — to nie jest strata pakietów)", kind: "no_ip") }
            sample = Sample(epoch: epoch, link: .noIP)
            detector.grace()
        } else {
            if lastLink == .noIP { event("IP wróciło: \(state.interfaceIP ?? "?") na \(interface)", kind: "ip_back") }
            let timeout = config.pingTimeout
            let routerHost = router
            async let r = Self.ping(routerHost, timeout: timeout)
            async let n = ICMPPinger.ping(config.nasIP, timeout: timeout)
            async let i = ICMPPinger.ping(config.internetIP, timeout: timeout)
            let (rr, nr, ir) = await (r, n, i)
            // Mac zaczął zasypiać w trakcie tej sekundy — wynik nie jest wiarygodny.
            guard !sleeping, !Task.isCancelled else { return }

            var link: LinkState = .up
            if let until = graceUntil {
                if rr.outcome == .ok {
                    event("Łączność z routerem po wybudzeniu: \(epoch - (wokeAt ?? epoch)) s", kind: "wake_ok")
                    graceUntil = nil
                } else if epoch < until {
                    link = .wake
                } else {
                    event("Brak łączności z routerem \(config.wakeGraceMax) s po wybudzeniu — dalej liczone jako zawieszenie", kind: "wake_timeout")
                    graceUntil = nil
                }
            }
            localNetworkBlocked = rr.outcome == .ok && nr.errorKind == "unreachable"
            sample = Sample(epoch: epoch, link: link, router: rr.rttMs, nas: nr.rttMs, internet: ir.rttMs)
            record(epoch: epoch, link: link, replies: [("gateway", router, rr), ("nas", config.nasIP, nr), ("internet", config.internetIP, ir)])
            if link == .up {
                handle(detector.feed(epoch: epoch, routerOK: rr.outcome == .ok, nasOK: nr.outcome == .ok, internetOK: ir.outcome == .ok),
                       nas: nr.rttMs, internet: ir.rttMs)
            } else {
                detector.grace()
            }
        }
        lastLink = sample.link

        samples.append(sample)
        if samples.count > Self.window { samples.removeFirst(samples.count - Self.window) }
        health = Health.evaluate(recent: samples, inIncident: detector.inIncident)
        let files = files
        Task { await files.ping(sample) }

        sampleWiFi(epoch: epoch, now: now)
        if epoch - lastFlush >= 5 {
            lastFlush = epoch
            Task { await store?.flush() }
        }
        if epoch - lastCount >= 60 {
            lastCount = epoch
            Task { unsent = await store?.unsentCount() ?? 0 }
        }
    }

    /// Brak znanego routera to błąd próby, nie strata pakietu.
    nonisolated private static func ping(_ host: String?, timeout: TimeInterval) async -> PingReply {
        guard let host else { return PingReply(outcome: .error, errorKind: "no_router") }
        return await ICMPPinger.ping(host, timeout: timeout)
    }

    private func record(epoch: Int, link: LinkState, replies: [(String, String?, PingReply)]) {
        guard let store else { return }
        let ms = Int64(epoch) * 1000
        let timeoutMs = Int(config.pingTimeout * 1000)
        let records = replies.map { ProbeRecord(epochMs: ms, target: $0.0, host: $0.1, reply: $0.2, timeoutMs: timeoutMs, link: link) }
        Task { await store.add(records) }
    }

    private func sampleWiFi(epoch: Int, now: Date) {
        // Popover: szybki odczyt CoreWLAN co 5 s, AWDL i MCS z ostatniej pełnej próbki.
        if epoch - lastWiFiDisplay >= 5 {
            lastWiFiDisplay = epoch
            var w = WiFiReader.read()
            w.awdl = wifi?.awdl ?? "none"
            w.mcs = wifi?.mcs
            wifi = w
        }
        guard epoch - lastWiFi >= config.wifiEvery else { return }
        lastWiFi = epoch
        let base = WiFiReader.read()
        let mcs = config.mcsFromSystemProfiler
        let files = files
        let store = store
        Task {
            let full = await WiFiReader.enrich(base, mcs: mcs)
            wifi = full
            await files.wifi(full, at: now)
            await store?.addWiFi(full, at: now)
        }
    }

    private func dailyCleanup(_ now: Date) {
        let day = Stamp.day(now)
        guard day != lastDay else { return }
        lastDay = day
        let files = files, store = store, keep = config.keepDays, storeKeep = config.storeKeepDays
        Task {
            await files.cleanup(keepDays: keep)
            await store?.prune(olderThanDays: storeKeep)
        }
    }

    // MARK: - Zdarzenia

    private func handle(_ outputs: [StuckDetector.Output], nas: Double? = nil, internet: Double? = nil) {
        func ms(_ v: Double?) -> String { v.map { String(format: "%.1fms", $0) } ?? "strata" }
        for o in outputs {
            switch o {
            case .started(let run, _, _):
                event("ZAWIESZENIE: router nie odpowiada od \(run) s (nas=\(ms(nas)) internet=\(ms(internet)))", kind: "stuck_start")
            case .snapshot(let run):
                takeSnapshot(reason: "router nie odpowiada od \(run) s")
            case .ended(let d, let lost):
                event("KONIEC zawieszenia: trwało \(d) s, utracone pingi do routera: \(lost)", kind: "stuck_end")
            case .interrupted(let d, let lost):
                event(Self.interruptedText(d, lost), kind: "stuck_interrupted")
            }
        }
    }

    private func takeSnapshot(reason: String) {
        var wifi = WiFiReader.read()
        wifi.awdl = self.wifi?.awdl ?? "?"
        wifi.mcs = self.wifi?.mcs
        let router = router, iface = interface, url = config.nasURL, files = files
        Task {
            let body = await Diagnostics.snapshot(router: router, interface: iface, nasURL: url, wifi: wifi)
            await files.snapshot(body, reason: reason)
        }
    }

    private func event(_ text: String, kind: String) {
        let now = Date()
        events.append(EventLine(date: now, text: text))
        if events.count > 30 { events.removeFirst(events.count - 30) }
        let files = files, store = store
        Task {
            await files.event(text, at: now)
            await store?.addEvent(kind: kind, detail: text, at: now)
        }
    }
}
