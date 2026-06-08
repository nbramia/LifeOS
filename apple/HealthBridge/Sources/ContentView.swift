import SwiftUI

struct ContentView: View {
    @AppStorage("serverURL") private var serverURL: String = "https://your-machine.tailXXXX.ts.net/api/fitness/health/ingest"
    @AppStorage("token") private var token: String = ""
    @StateObject private var engine = SyncEngine.shared

    var body: some View {
        NavigationStack {
            Form {
                Section("LifeOS server") {
                    TextField("Ingest URL", text: $serverURL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                    SecureField("Ingest token", text: $token)
                }

                Section("Sync") {
                    Button {
                        Task { await engine.syncNow(serverURL: serverURL, token: token) }
                    } label: {
                        HStack {
                            Text("Sync now")
                            if engine.isSyncing { Spacer(); ProgressView() }
                        }
                    }
                    .disabled(engine.isSyncing)

                    if let last = engine.lastSync {
                        LabeledContent("Last sync", value: last.formatted(date: .abbreviated, time: .shortened))
                    }
                    Text(engine.status).font(.footnote).foregroundStyle(.secondary)
                }

                Section("Maintenance") {
                    Button("Re-authorize Health access") {
                        Task { await engine.requestAuthorization() }
                    }
                    Button("Reset anchors (re-export history)", role: .destructive) {
                        engine.resetAnchors()
                    }
                }
            }
            .navigationTitle("HealthBridge")
            .task {
                await engine.requestAuthorization()
                engine.startBackgroundDelivery(serverURL: serverURL, token: token)
            }
        }
    }
}

#Preview {
    ContentView()
}
