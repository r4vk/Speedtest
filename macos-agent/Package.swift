// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "NetLog",
    platforms: [.macOS(.v26)],
    targets: [
        // Pomiary, reguły i zapis — bez SwiftUI, więc testy działają bez aplikacji.
        .target(
            name: "NetLogKit",
            linkerSettings: [
                .linkedFramework("CoreWLAN"),
                .linkedFramework("SystemConfiguration"),
                .linkedLibrary("sqlite3"),
            ]
        ),
        .executableTarget(
            name: "NetLog",
            dependencies: ["NetLogKit"],
            // Kod aplikacji działa na wątku głównym, chyba że zaznaczono inaczej.
            swiftSettings: [.defaultIsolation(MainActor.self)]
        ),
        // Testy jako program: Swift Testing i XCTest wymagają pełnego Xcode, Command Line Tools wystarczą tu.
        .executableTarget(name: "NetLogCheck", dependencies: ["NetLogKit"], path: "Tests/NetLogCheck"),
    ]
)
