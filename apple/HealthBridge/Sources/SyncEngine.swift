import Foundation
import HealthKit

/// Orchestrates a sync: authorize → collect (incremental) → POST to LifeOS.
/// Also wires HealthKit background delivery so new samples sync without a manual
/// launch. Observable so the UI can show status.
@MainActor
final class SyncEngine: ObservableObject {
    static let shared = SyncEngine()

    @Published var status: String = "Idle"
    @Published var lastSync: Date?
    @Published var isSyncing = false

    private let hk = HealthKitManager.shared
    private var observersStarted = false

    func requestAuthorization() async {
        do {
            try await hk.requestAuthorization()
            status = "Authorized"
        } catch {
            status = "Auth failed: \(error.localizedDescription)"
        }
    }

    /// Run one sync. `serverURL` is the full ingest URL; `token` the bearer.
    func syncNow(serverURL: String, token: String) async {
        guard !serverURL.isEmpty, !token.isEmpty else {
            status = "Set the server URL and token first"
            return
        }
        isSyncing = true
        status = "Collecting…"
        let (payload, pendingAnchors) = await hk.collect()
        let count = payload.workouts.count + payload.metrics.count
        if count == 0 {
            status = "Nothing new to sync"
            lastSync = Date()
            isSyncing = false
            return
        }
        status = "Uploading \(count) items…"
        do {
            let body = try await Uploader.post(payload, to: serverURL, token: token)
            // Commit anchors ONLY after a confirmed upload, so a failed POST
            // re-reads these samples next time (the server is idempotent).
            hk.commitAnchors(pendingAnchors)
            status = "Synced \(count) items. \(body)"
            lastSync = Date()
        } catch {
            status = "Upload failed (will retry these samples): \(error.localizedDescription)"
        }
        isSyncing = false
    }

    /// Enable background delivery + observer queries so iOS wakes the app to sync.
    /// Requires the HealthKit background-delivery entitlement. Credentials are read
    /// from UserDefaults at fire time so editing them in the UI takes effect without
    /// an app restart.
    func startBackgroundDelivery() {
        guard !observersStarted else { return }
        observersStarted = true
        for type in hk.backgroundTypes() {
            hk.store.enableBackgroundDelivery(for: type, frequency: .hourly) { ok, error in
                if let error { print("enableBackgroundDelivery(\(type)) error: \(error)") }
                _ = ok
            }
            let observer = HKObserverQuery(sampleType: type, predicate: nil) { [weak self] _, completion, error in
                if let error { print("observer(\(type)) error: \(error)") }
                Task { @MainActor in
                    let url = UserDefaults.standard.string(forKey: "serverURL") ?? ""
                    let token = UserDefaults.standard.string(forKey: "token") ?? ""
                    await self?.syncNow(serverURL: url, token: token)
                    completion()
                }
            }
            hk.store.execute(observer)
        }
    }

    func resetAnchors() {
        AnchorStore.reset()
        status = "Anchors reset — next sync re-exports history"
    }
}
