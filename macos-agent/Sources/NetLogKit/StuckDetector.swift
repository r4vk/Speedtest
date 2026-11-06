import Foundation

/// Stan łącza w danej sekundzie — kolumna `link` pliku `ping-*.csv`.
public enum LinkState: String, Sendable, Codable {
    /// Jest adres IP, pomiar normalny.
    case up
    /// Brak adresu IP (Wi-Fi wyłączone, roaming) — to nie jest strata pakietów.
    case noIP = "no_ip"
    /// Pierwsze sekundy po wybudzeniu: Wi-Fi łączy się ponownie, straty nie otwierają zawieszenia.
    case wake
}

/// Wynik jednej sekundy pomiarów.
public struct Sample: Sendable, Equatable, Identifiable {
    public var epoch: Int
    public var link: LinkState
    public var router: Double?
    public var nas: Double?
    public var internet: Double?
    public var id: Int { epoch }

    public init(epoch: Int, link: LinkState, router: Double? = nil, nas: Double? = nil, internet: Double? = nil) {
        self.epoch = epoch
        self.link = link
        self.router = router
        self.nas = nas
        self.internet = internet
    }

    /// Wiersz CSV po `ts` (`link,router_ms,nas_ms,internet_ms`); pusta wartość = brak odpowiedzi.
    public var csvFields: String {
        func f(_ v: Double?) -> String { v.map { String(format: "%.3f", $0) } ?? "" }
        return "\(link.rawValue),\(f(router)),\(f(nas)),\(f(internet))"
    }
}

/// Wykrywanie zawieszenia: router nie odpowiada `stuckAfter` sekund z rzędu.
/// Te same reguły co w netlog.sh, plus jawne przerwanie przy uśpieniu Maca.
public struct StuckDetector: Sendable {
    public enum Output: Sendable, Equatable {
        case started(lossRun: Int, nasOK: Bool, internetOK: Bool)
        case snapshot(lossRun: Int)
        case ended(durationS: Int, lost: Int)
        /// Zawieszenie przerwane uśpieniem: czas liczony tylko do chwili uśpienia.
        case interrupted(durationS: Int, lost: Int)
    }

    public let stuckAfter: Int
    public let snapshotEvery: Int
    public private(set) var lossRun = 0
    public private(set) var incidentStart: Int?
    public private(set) var incidentLost = 0
    private var lastSnapshot: Int?

    public init(stuckAfter: Int = 3, snapshotEvery: Int = 30) {
        self.stuckAfter = stuckAfter
        self.snapshotEvery = snapshotEvery
    }

    public var inIncident: Bool { incidentStart != nil }

    /// Sekunda z pomiarem (link `up`).
    public mutating func feed(epoch: Int, routerOK: Bool, nasOK: Bool, internetOK: Bool) -> [Output] {
        var out: [Output] = []
        if routerOK {
            if let start = incidentStart {
                out.append(.ended(durationS: epoch - start, lost: incidentLost))
                incidentStart = nil
                incidentLost = 0
            }
            lossRun = 0
            return out
        }
        lossRun += 1
        guard lossRun >= stuckAfter else { return out }
        if incidentStart == nil {
            incidentStart = epoch - stuckAfter + 1
            incidentLost = stuckAfter - 1
            out.append(.started(lossRun: lossRun, nasOK: nasOK, internetOK: internetOK))
        }
        incidentLost += 1
        if lastSnapshot.map({ epoch - $0 >= snapshotEvery }) ?? true {
            lastSnapshot = epoch
            out.append(.snapshot(lossRun: lossRun))
        }
        return out
    }

    /// Sekunda okresu łaski po wybudzeniu: straty nie liczą się do zawieszenia.
    public mutating func grace() { lossRun = 0 }

    /// Mac zasypia albo pomiary stają: zamyka otwarte zawieszenie i zeruje licznik.
    public mutating func pause(epoch: Int) -> [Output] {
        var out: [Output] = []
        if let start = incidentStart {
            out.append(.interrupted(durationS: max(0, epoch - start), lost: incidentLost))
        }
        incidentStart = nil
        incidentLost = 0
        lossRun = 0
        lastSnapshot = nil
        return out
    }
}

/// Stan do ikony w pasku menu.
public enum Health: String, Sendable {
    case ok, lag, stuck, noIP, waking, sleeping, starting

    /// Ocena z ostatnich sekund. Progi: patrz README („Kolory ikony”).
    public static func evaluate(recent: [Sample], inIncident: Bool, window: Int = 10) -> Health {
        if inIncident { return .stuck }
        guard let last = recent.last else { return .starting }
        switch last.link {
        case .noIP: return .noIP
        case .wake: return .waking
        case .up: break
        }
        let tail = recent.suffix(window).filter { $0.link == .up }
        let routerLoss = tail.filter { $0.router == nil }.count
        let nasLoss = tail.filter { $0.nas == nil }.count
        let internetLoss = tail.filter { $0.internet == nil }.count
        let slowRouter = tail.filter { ($0.router ?? 0) > 100 }.count
        let slowInternet = tail.filter { ($0.internet ?? 0) > 200 }.count
        if routerLoss > 0 || nasLoss > 1 || internetLoss > 1 || slowRouter > 1 || slowInternet > 1 {
            return .lag
        }
        return .ok
    }
}
