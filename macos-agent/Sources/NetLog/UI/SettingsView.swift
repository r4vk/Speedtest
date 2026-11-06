import NetLogKit
import Observation
import SwiftUI

/// Szkic ustawień edytowany w oknie; trafia do `Monitor.apply` dopiero po „Zapisz”.
@Observable
final class SettingsDraft {
    var nasIP: String
    var nasURL: String
    var internetIP: String
    var routerIP: String      // puste = automatycznie
    var mcs: Bool
    var keepDays: Int
    var storeKeepDays: Int

    init(_ c: Config) {
        nasIP = c.nasIP
        nasURL = c.nasURL
        internetIP = c.internetIP
        routerIP = c.routerIP ?? ""
        mcs = c.mcsFromSystemProfiler
        keepDays = c.keepDays
        storeKeepDays = c.storeKeepDays
    }

    var errors: [String] {
        var e: [String] = []
        if !Config.isValidHost(nasIP) { e.append("Adres NAS-a: podaj IP lub nazwę hosta.") }
        if !Config.isValidURL(nasURL) { e.append("Panel NAS: podaj adres http:// lub https://.") }
        if !Config.isValidHost(internetIP) { e.append("Cel w internecie: podaj IP lub nazwę hosta.") }
        if !routerIP.trimmingCharacters(in: .whitespaces).isEmpty && !Config.isValidHost(routerIP) {
            e.append("Router: podaj IP albo zostaw puste (automatycznie).")
        }
        return e
    }

    func config(base: Config) -> Config {
        var c = base
        c.nasIP = nasIP.trimmingCharacters(in: .whitespaces)
        c.nasURL = nasURL.trimmingCharacters(in: .whitespaces)
        c.internetIP = internetIP.trimmingCharacters(in: .whitespaces)
        let r = routerIP.trimmingCharacters(in: .whitespaces)
        c.routerIP = r.isEmpty ? nil : r
        c.mcsFromSystemProfiler = mcs
        c.keepDays = keepDays
        c.storeKeepDays = storeKeepDays
        return c
    }

    func resetToDefaults() {
        let d = Config.load(env: [:], defaults: UserDefaults(suiteName: "org.r4vk.netlog.defaults-only") ?? .standard)
        nasIP = d.nasIP
        nasURL = d.nasURL
        internetIP = d.internetIP
        routerIP = ""
        mcs = d.mcsFromSystemProfiler
        keepDays = d.keepDays
        storeKeepDays = d.storeKeepDays
    }
}

/// Okno ustawień: cele pomiarów, przechowywanie danych, start przy logowaniu.
struct SettingsView: View {
    @Environment(Monitor.self) private var monitor
    let draft: SettingsDraft
    let close: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            Form {
                Section {
                    TextField("NAS", text: bind(\.nasIP), prompt: Text("192.168.10.2"))
                    TextField("Panel NAS", text: bind(\.nasURL), prompt: Text("http://192.168.10.2:18000/"))
                    TextField("Internet", text: bind(\.internetIP), prompt: Text("1.1.1.1"))
                    TextField("Router", text: bind(\.routerIP), prompt: Text("automatycznie (\(monitor.net.router ?? "brak"))"))
                } header: {
                    Text("Cele pomiarów")
                } footer: {
                    Text("Ping co sekundę, próg 1 s. Router zostaw pusty, żeby wykrywać go z trasy domyślnej.")
                        .foregroundStyle(.secondary)
                }

                Section("Wi-Fi") {
                    Toggle(isOn: bind(\.mcs)) {
                        Text("MCS z system_profiler co minutę")
                        Text("CoreWLAN nie podaje MCS. Wyłącz, jeśli nie potrzebujesz tej kolumny.")
                    }
                }

                Section("Przechowywanie") {
                    Stepper(value: bind(\.keepDays), in: 1...365) {
                        LabeledContent("Pliki CSV i dziennik", value: "\(draft.keepDays) dni")
                    }
                    Stepper(value: bind(\.storeKeepDays), in: 1...60) {
                        LabeledContent("Baza SQLite (kolejka na NAS)", value: "\(draft.storeKeepDays) dni")
                    }
                    LabeledContent("Folder logów") {
                        Button("Pokaż w Finderze") { NSWorkspace.shared.open(monitor.config.logDir) }
                    }
                }

                Section("Ogólne") {
                    Toggle("Uruchamiaj przy logowaniu",
                           isOn: Binding(get: { monitor.launchAtLogin }, set: { monitor.setLaunchAtLogin($0) }))
                }

                if !Config.envOverrides().isEmpty {
                    Section {
                        Label("Zmienne środowiskowe nadpisują część ustawień: \(Config.envOverrides().joined(separator: ", ")).",
                              systemImage: "exclamationmark.triangle")
                            .foregroundStyle(.orange)
                    }
                }
            }
            .formStyle(.grouped)
            // Form to lista przewijana bez własnej wysokości: w oknie AppKit zwijała się do zera.
            .frame(minHeight: 420, idealHeight: 660)

            VStack(alignment: .leading, spacing: 8) {
                ForEach(draft.errors, id: \.self) { e in
                    Label(e, systemImage: "xmark.circle").foregroundStyle(.red).font(.callout)
                }
                HStack {
                    Button("Przywróć domyślne") { draft.resetToDefaults() }
                    Spacer()
                    Button("Anuluj", role: .cancel) { close() }
                        .keyboardShortcut(.cancelAction)
                    Button("Zapisz") {
                        monitor.apply(draft.config(base: monitor.config))
                        close()
                    }
                    .keyboardShortcut(.defaultAction)
                    .buttonStyle(.borderedProminent)
                    .disabled(!draft.errors.isEmpty)
                }
            }
            .padding(.horizontal, 20)
            .padding(.bottom, 16)
        }
        .frame(width: 480)
    }

    private func bind<T>(_ key: ReferenceWritableKeyPath<SettingsDraft, T>) -> Binding<T> {
        Binding(get: { draft[keyPath: key] }, set: { draft[keyPath: key] = $0 })
    }
}
