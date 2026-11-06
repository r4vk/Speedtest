import CoreWLAN
import Foundation

/// Stan Wi-Fi w kolumnach pliku `wifi-*.csv` z netlog.sh.
public struct WiFiSample: Sendable, Equatable {
    public var interface: String
    public var powerOn: Bool
    public var rssi: Int?
    public var noise: Int?
    public var channel: Int?
    public var band: String?      // "2GHz", "5GHz", "6GHz" — jak system_profiler
    public var width: String?     // "20MHz" … "320MHz"
    public var txRate: Double?    // Mb/s
    public var mcs: Int?
    public var phy: String?       // "802.11ax"
    public var awdl: String       // "active", "inactive", "none"

    public init(interface: String = "en0", powerOn: Bool = false, rssi: Int? = nil, noise: Int? = nil,
                channel: Int? = nil, band: String? = nil, width: String? = nil, txRate: Double? = nil,
                mcs: Int? = nil, phy: String? = nil, awdl: String = "none") {
        self.interface = interface
        self.powerOn = powerOn
        self.rssi = rssi
        self.noise = noise
        self.channel = channel
        self.band = band
        self.width = width
        self.txRate = txRate
        self.mcs = mcs
        self.phy = phy
        self.awdl = awdl
    }

    public static let csvHeader = "ts,rssi_dbm,noise_dbm,channel,band,width,tx_rate_mbps,mcs,phy,awdl"

    /// Kolumny po `ts`, w formacie netlog.sh.
    public var csvFields: String {
        func s<T>(_ v: T?) -> String { v.map { "\($0)" } ?? "" }
        let tx = txRate.map { $0.rounded() == $0 ? String(Int($0)) : String(format: "%.1f", $0) } ?? ""
        return [s(rssi), s(noise), s(channel), s(band), s(width), tx, s(mcs), s(phy), awdl].joined(separator: ",")
    }

    /// Krótki opis do zrzutu i popovera.
    public var summary: String {
        guard powerOn else { return "Wi-Fi wyłączone" }
        guard let rssi else { return "Wi-Fi bez połączenia" }
        var parts = ["RSSI \(rssi) dBm"]
        if let noise { parts.append("szum \(noise) dBm") }
        if let channel { parts.append("kanał \(channel) \(band ?? "") \(width ?? "")".trimmingCharacters(in: .whitespaces)) }
        if let txRate { parts.append("tx \(Int(txRate)) Mb/s") }
        if let mcs { parts.append("MCS \(mcs)") }
        if let phy { parts.append(phy) }
        parts.append("AWDL \(awdl)")
        return parts.joined(separator: " · ")
    }
}

public enum WiFiReader {
    /// Nazwa interfejsu Wi-Fi (zwykle en0).
    public static func interfaceName() -> String {
        CWWiFiClient.shared().interface()?.interfaceName ?? "en0"
    }

    /// Szybki odczyt z CoreWLAN. AWDL i MCS uzupełnia wywołujący (wymagają procesów zewnętrznych).
    public static func read() -> WiFiSample {
        guard let i = CWWiFiClient.shared().interface() else { return WiFiSample() }
        var s = WiFiSample(interface: i.interfaceName ?? "en0", powerOn: i.powerOn())
        let rssi = i.rssiValue()
        guard s.powerOn, rssi != 0 else { return s }
        s.rssi = rssi
        let noise = i.noiseMeasurement()
        s.noise = noise == 0 ? nil : noise
        if let ch = i.wlanChannel() {
            s.channel = ch.channelNumber
            s.band = band(ch.channelBand)
            s.width = width(ch.channelWidth)
        }
        let tx = i.transmitRate()
        s.txRate = tx > 0 ? tx : nil
        s.phy = phy(i.activePHYMode())
        return s
    }

    static func band(_ b: CWChannelBand) -> String? {
        switch b {
        case .band2GHz: "2GHz"
        case .band5GHz: "5GHz"
        case .band6GHz: "6GHz"
        default: nil
        }
    }

    static func width(_ w: CWChannelWidth) -> String? {
        switch w {
        case .width20MHz: "20MHz"
        case .width40MHz: "40MHz"
        case .width80MHz: "80MHz"
        case .width160MHz: "160MHz"
        default: nil
        }
    }

    static func phy(_ m: CWPHYMode) -> String? {
        switch m {
        case .mode11a: "802.11a"
        case .mode11b: "802.11b"
        case .mode11g: "802.11g"
        case .mode11n: "802.11n"
        case .mode11ac: "802.11ac"
        case .mode11ax: "802.11ax"
        default: m.rawValue == 0 ? nil : "802.11(\(m.rawValue))"
        }
    }
}

extension WiFiReader {
    /// Uzupełnia próbkę o AWDL (`ifconfig awdl0`) i opcjonalnie MCS (`system_profiler`) — poza wątkiem głównym.
    @concurrent
    public static func enrich(_ sample: WiFiSample, mcs: Bool) async -> WiFiSample {
        var s = sample
        s.awdl = Shell.awdlStatus()
        if mcs, s.rssi != nil { s.mcs = Shell.parseMCS(Shell.airportCurrentNetwork()) }
        return s
    }

    /// Czy działa jeszcze stary logger powłoki (LaunchAgent org.r4vk.netlog)?
    @concurrent
    public static func legacyLoggerRunning() async -> Bool {
        Shell.run("/bin/launchctl", ["print", "gui/\(getuid())/org.r4vk.netlog"], timeout: 3).contains("state = running")
    }
}
