import Foundation
import Security

/// Token wysyłki trzymany w pęku kluczy użytkownika (nigdy w `defaults`).
/// Przy podpisie ad hoc macOS może po przebudowie zapytać, czy NetLog może odczytać ten wpis.
public enum Keychain {
    public static let service = "org.r4vk.netlog"
    public static let ingestTokenAccount = "ingest-token"

    private static func base(_ account: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: account]
    }

    public static func read(_ account: String = ingestTokenAccount) -> String? {
        var q = base(account)
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let data = out as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    @discardableResult
    public static func save(_ value: String, account: String = ingestTokenAccount) -> OSStatus {
        delete(account)
        var q = base(account)
        q[kSecValueData as String] = Data(value.utf8)
        q[kSecAttrLabel as String] = "NetLog — token wysyłki wyników na NAS"
        return SecItemAdd(q as CFDictionary, nil)
    }

    @discardableResult
    public static func delete(_ account: String = ingestTokenAccount) -> OSStatus {
        SecItemDelete(base(account) as CFDictionary)
    }
}
