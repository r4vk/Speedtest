import Foundation

/// Zrzut diagnostyczny przy zawieszeniu. Te same sekcje co w netlog.sh: Wi-Fi, interfejs, AWDL,
/// ARP routera, trasa domyślna, HTTP do NAS-a, DNS.
public enum Diagnostics {
    @concurrent
    public static func snapshot(router: String?, interface: String, nasURL: String, wifi: WiFiSample) async -> String {
        var out = ""
        out += "--- Wi-Fi (CoreWLAN)\n\(wifi.summary)\n"
        out += "--- Wi-Fi (system_profiler)\n\(Shell.airportCurrentNetwork())\n"
        out += "--- ifconfig \(interface)\n"
        out += Shell.run("/sbin/ifconfig", [interface]).split(separator: "\n")
            .filter { $0.contains("status") || $0.contains("inet ") || $0.contains("ether") || $0.contains("media") }
            .joined(separator: "\n") + "\n"
        out += "--- awdl0: \(Shell.awdlStatus())\n"
        out += "--- ARP routera (\(router ?? "?"))\n"
        out += router.map { Shell.run("/usr/sbin/arp", ["-n", $0], timeout: 3) } ?? "brak routera\n"
        out += "--- trasa domyślna\n"
        out += Shell.run("/sbin/route", ["-n", "get", "default"], timeout: 3).split(separator: "\n")
            .filter { $0.contains("gateway") || $0.contains("interface") }
            .joined(separator: "\n") + "\n"
        out += "--- HTTP do NAS (\(nasURL))\n\(await http(nasURL))\n"
        out += "--- DNS\n"
        let dns = Shell.run("/usr/sbin/scutil", ["--dns"], timeout: 3).split(separator: "\n")
            .filter { $0.contains("nameserver[") }
            .map { $0.trimmingCharacters(in: .whitespaces) }
        out += Array(Set(dns)).sorted().prefix(4).joined(separator: "\n") + "\n"
        return out
    }

    static func http(_ url: String) async -> String {
        guard let u = URL(string: url) else { return "zły adres" }
        var req = URLRequest(url: u, cachePolicy: .reloadIgnoringLocalAndRemoteCacheData, timeoutInterval: 3)
        req.httpMethod = "GET"
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForResource = 3
        let session = URLSession(configuration: config)
        defer { session.invalidateAndCancel() }
        let t0 = Date()
        do {
            let (_, resp) = try await session.data(for: req)
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            return String(format: "http=%d czas=%.3fs", code, Date().timeIntervalSince(t0))
        } catch {
            return String(format: "nieudane po %.3fs: %@", Date().timeIntervalSince(t0), error.localizedDescription)
        }
    }
}
