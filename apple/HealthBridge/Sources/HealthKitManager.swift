import Foundation
import HealthKit

/// Reads HealthKit incrementally (via persisted anchors) and builds the
/// `ExportPayload`. Read-only — HealthBridge never writes health data.
final class HealthKitManager {
    static let shared = HealthKitManager()
    let store = HKHealthStore()

    private let iso: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    // Quantity metrics to export: (HK identifier, payload "type", unit, unit label).
    private struct MetricSpec {
        let id: HKQuantityTypeIdentifier
        let type: String
        let unit: HKUnit
        let label: String
    }

    private let metricSpecs: [MetricSpec] = [
        .init(id: .bodyMass, type: "body_weight", unit: .pound(), label: "lb"),
        .init(id: .restingHeartRate, type: "resting_hr", unit: HKUnit.count().unitDivided(by: .minute()), label: "bpm"),
        .init(id: .heartRateVariabilitySDNN, type: "hrv", unit: .secondUnit(with: .milli), label: "ms"),
        .init(id: .stepCount, type: "steps", unit: .count(), label: "count"),
        .init(id: .activeEnergyBurned, type: "active_energy", unit: .kilocalorie(), label: "kcal"),
    ]

    // MARK: Authorization

    func requestAuthorization() async throws {
        guard HKHealthStore.isHealthDataAvailable() else {
            throw NSError(domain: "HealthBridge", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "Health data is not available on this device."])
        }
        var readTypes: Set<HKObjectType> = [HKWorkoutType.workoutType(),
                                            HKCategoryType(.sleepAnalysis),
                                            HKQuantityType(.heartRate)]
        for spec in metricSpecs { readTypes.insert(HKQuantityType(spec.id)) }
        try await store.requestAuthorization(toShare: [], read: readTypes)
    }

    /// Types we observe for background delivery.
    func backgroundTypes() -> [HKSampleType] {
        var types: [HKSampleType] = [HKWorkoutType.workoutType(), HKCategoryType(.sleepAnalysis)]
        for spec in metricSpecs { types.append(HKQuantityType(spec.id)) }
        return types
    }

    // MARK: Collection

    /// Collected payload plus the anchors to persist *only after* a successful
    /// upload (so a failed POST re-reads those samples next time — the server is
    /// idempotent, so re-sends are harmless).
    typealias Collected = (payload: ExportPayload, pendingAnchors: [(String, HKQueryAnchor)])

    func collect() async -> Collected {
        async let workouts = collectWorkouts()
        async let quantityMetrics = collectQuantityMetrics()
        async let sleep = collectSleep()
        let (w, wA) = await workouts
        let (q, qA) = await quantityMetrics
        let (s, sA) = await sleep
        let payload = ExportPayload(generatedAt: iso.string(from: Date()),
                                    workouts: w, metrics: q + s)
        return (payload, wA + qA + sA)
    }

    /// Persist anchors after a confirmed upload.
    func commitAnchors(_ pending: [(String, HKQueryAnchor)]) {
        for (key, anchor) in pending { AnchorStore.save(key, anchor) }
    }

    private func collectWorkouts() async -> ([WorkoutSample], [(String, HKQueryAnchor)]) {
        let (samples, pending) = await runAnchored(type: HKWorkoutType.workoutType(), anchorKey: "workouts")
        var out: [WorkoutSample] = []
        for case let w as HKWorkout in samples {
            let dist = w.statistics(for: HKQuantityType(.distanceWalkingRunning))?
                .sumQuantity()?.doubleValue(for: .meter())
            let energy = w.statistics(for: HKQuantityType(.activeEnergyBurned))?
                .sumQuantity()?.doubleValue(for: .kilocalorie())
            let avgHr = w.statistics(for: HKQuantityType(.heartRate))?
                .averageQuantity()?.doubleValue(for: HKUnit.count().unitDivided(by: .minute()))
            out.append(WorkoutSample(
                uuid: w.uuid.uuidString,
                type: Self.activityName(w.workoutActivityType),
                start: iso.string(from: w.startDate),
                end: iso.string(from: w.endDate),
                durationS: w.duration,
                distanceM: dist,
                energyKcal: energy,
                avgHr: avgHr
            ))
        }
        return (out, pending.map { [$0] } ?? [])
    }

    private func collectQuantityMetrics() async -> ([MetricSample], [(String, HKQueryAnchor)]) {
        var out: [MetricSample] = []
        var anchors: [(String, HKQueryAnchor)] = []
        for spec in metricSpecs {
            let (samples, pending) = await runAnchored(type: HKQuantityType(spec.id), anchorKey: spec.type)
            if let pending { anchors.append(pending) }
            for case let q as HKQuantitySample in samples {
                out.append(MetricSample(
                    type: spec.type,
                    value: q.quantity.doubleValue(for: spec.unit),
                    unit: spec.label,
                    start: iso.string(from: q.startDate),
                    end: iso.string(from: q.endDate)
                ))
            }
        }
        return (out, anchors)
    }

    /// Aggregate asleep category samples into one `sleep_hours` metric per night.
    /// The metric `start` is the night's UTC date at midnight — a STABLE key — so
    /// re-aggregating a night across multiple syncs dedupes to one row instead of
    /// emitting a new row per differing first-sample time. (A night still being
    /// filled in across syncs can undercount until the importer supports upsert.)
    private func collectSleep() async -> ([MetricSample], [(String, HKQueryAnchor)]) {
        let (samples, pending) = await runAnchored(type: HKCategoryType(.sleepAnalysis), anchorKey: "sleep")
        let asleep: Set<Int> = [
            HKCategoryValueSleepAnalysis.asleepUnspecified.rawValue,
            HKCategoryValueSleepAnalysis.asleepCore.rawValue,
            HKCategoryValueSleepAnalysis.asleepDeep.rawValue,
            HKCategoryValueSleepAnalysis.asleepREM.rawValue,
        ]
        var perNight: [String: (seconds: Double, end: Date)] = [:]
        for case let s as HKCategorySample in samples where asleep.contains(s.value) {
            let dayKey = ISO8601DateFormatter.dateOnly.string(from: s.startDate)  // UTC date
            let dur = s.endDate.timeIntervalSince(s.startDate)
            if var night = perNight[dayKey] {
                night.seconds += dur
                night.end = max(night.end, s.endDate)
                perNight[dayKey] = night
            } else {
                perNight[dayKey] = (dur, s.endDate)
            }
        }
        let metrics = perNight.map { dayKey, night in
            MetricSample(type: "sleep_hours",
                         value: (night.seconds / 3600).rounded(toPlaces: 2),
                         unit: "h",
                         start: "\(dayKey)T00:00:00Z",  // stable per-night key
                         end: iso.string(from: night.end))
        }
        return (metrics, pending.map { [$0] } ?? [])
    }

    // MARK: Anchored query helper

    /// Returns the new samples and the (key, newAnchor) to persist — but does NOT
    /// persist it. The caller commits anchors only after a successful upload so a
    /// failed delivery re-reads the same samples next time.
    private func runAnchored(type: HKSampleType, anchorKey: String) async -> ([HKSample], (String, HKQueryAnchor)?) {
        await withCheckedContinuation { continuation in
            let query = HKAnchoredObjectQuery(
                type: type,
                predicate: nil,
                anchor: AnchorStore.load(anchorKey),
                limit: HKObjectQueryNoLimit
            ) { _, samples, _, newAnchor, error in
                if let error { print("HealthBridge anchored query (\(anchorKey)) error: \(error)") }
                let pending = newAnchor.map { (anchorKey, $0) }
                continuation.resume(returning: (samples ?? [], pending))
            }
            store.execute(query)
        }
    }

    // MARK: Activity name

    static func activityName(_ t: HKWorkoutActivityType) -> String {
        switch t {
        case .running: return "Running"
        case .walking: return "Walking"
        case .cycling: return "Cycling"
        case .swimming: return "Swimming"
        case .rowing: return "Rowing"
        case .elliptical: return "Elliptical"
        case .hiking: return "Hiking"
        case .highIntensityIntervalTraining: return "HighIntensityIntervalTraining"
        case .functionalStrengthTraining: return "FunctionalStrengthTraining"
        case .traditionalStrengthTraining: return "TraditionalStrengthTraining"
        case .yoga: return "Yoga"
        case .coreTraining: return "CoreTraining"
        case .flexibility: return "Flexibility"
        default: return "Workout"
        }
    }
}

private extension ISO8601DateFormatter {
    static let dateOnly: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withFullDate]
        return f
    }()
}

private extension Double {
    func rounded(toPlaces places: Int) -> Double {
        let p = pow(10.0, Double(places))
        return (self * p).rounded() / p
    }
}
