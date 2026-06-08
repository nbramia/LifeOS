import Foundation
import HealthKit

/// Persists one `HKQueryAnchor` per sample type so each sync emits only new
/// samples (incremental). Anchors survive app restarts via UserDefaults.
enum AnchorStore {
    private static let defaults = UserDefaults.standard
    private static let prefix = "healthbridge.anchor."

    static func load(_ key: String) -> HKQueryAnchor? {
        guard let data = defaults.data(forKey: prefix + key) else { return nil }
        return try? NSKeyedUnarchiver.unarchivedObject(ofClass: HKQueryAnchor.self, from: data)
    }

    static func save(_ key: String, _ anchor: HKQueryAnchor?) {
        guard let anchor else { return }
        if let data = try? NSKeyedArchiver.archivedData(withRootObject: anchor, requiringSecureCoding: true) {
            defaults.set(data, forKey: prefix + key)
        }
    }

    /// Clear all anchors — forces a full re-export on the next sync (history backfill).
    static func reset() {
        for key in defaults.dictionaryRepresentation().keys where key.hasPrefix(prefix) {
            defaults.removeObject(forKey: key)
        }
    }
}
