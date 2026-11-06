import Foundation
import SystemConfiguration

/// Stan sieci z SystemConfiguration: router z trasy domyślnej i adres IPv4 interfejsu Wi-Fi.
/// Zastępuje `route -n get default` i `ipconfig getifaddr en0` z netlog.sh.
public struct NetState: Sendable, Equatable {
    public var router: String?
    public var primaryInterface: String?
    public var interfaceIP: String?

    public init(router: String? = nil, primaryInterface: String? = nil, interfaceIP: String? = nil) {
        self.router = router
        self.primaryInterface = primaryInterface
        self.interfaceIP = interfaceIP
    }
}

public enum NetInfo {
    public static func current(interface: String) -> NetState {
        guard let store = SCDynamicStoreCreate(nil, "NetLog" as CFString, nil, nil) else { return NetState() }
        let global = SCDynamicStoreCopyValue(store, "State:/Network/Global/IPv4" as CFString) as? [String: Any]
        let iface = SCDynamicStoreCopyValue(store, "State:/Network/Interface/\(interface)/IPv4" as CFString) as? [String: Any]
        return NetState(
            router: global?["Router"] as? String,
            primaryInterface: global?["PrimaryInterface"] as? String,
            interfaceIP: (iface?["Addresses"] as? [String])?.first
        )
    }
}
