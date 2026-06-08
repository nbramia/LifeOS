import Foundation

/// The exact `health.json` schema the LifeOS importer consumes (#323/#333).
/// Keep these field names in lockstep with `api/services/health_import.py`.
struct ExportPayload: Codable {
    var generatedAt: String
    var workouts: [WorkoutSample]
    var metrics: [MetricSample]

    enum CodingKeys: String, CodingKey {
        case generatedAt = "generated_at"
        case workouts
        case metrics
    }
}

struct WorkoutSample: Codable {
    let uuid: String        // HKWorkout UUID — the importer's dedupe key (required)
    let type: String        // friendly activity name, e.g. "Running"
    let start: String       // ISO-8601
    let end: String
    let durationS: Double
    let distanceM: Double?
    let energyKcal: Double?
    let avgHr: Double?

    enum CodingKeys: String, CodingKey {
        case uuid, type, start, end
        case durationS = "duration_s"
        case distanceM = "distance_m"
        case energyKcal = "energy_kcal"
        case avgHr = "avg_hr"
    }
}

struct MetricSample: Codable {
    let type: String        // e.g. "body_weight", "resting_hr", "hrv", "sleep_hours"
    let value: Double
    let unit: String
    let start: String       // ISO-8601 — required (the importer skips start-less metrics)
    let end: String?
}
