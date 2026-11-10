import SwiftUI

@main
struct HealthBridgeApp: App {
    init() {
        // Server settings preset at build time (Secrets.xcconfig → Info.plist) are the
        // defaults; anything typed in the app overrides them.
        let info = Bundle.main.infoDictionary ?? [:]
        var defaults: [String: Any] = [:]
        if let url = info["HBIngestURL"] as? String, !url.isEmpty { defaults["serverURL"] = url }
        if let token = info["HBIngestToken"] as? String, !token.isEmpty { defaults["token"] = token }
        UserDefaults.standard.register(defaults: defaults)
        // Observers must be registered at every launch, including the background
        // launches HealthKit makes to deliver new samples.
        SyncEngine.shared.startBackgroundDelivery()
    }

    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}
