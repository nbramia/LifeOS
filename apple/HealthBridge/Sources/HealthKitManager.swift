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

    func collect() async -> ExportPayload {
        async let workouts = collectWorkouts()
        async let quantityMetrics = collectQuantityMetrics()
        async let sleep = collectSleep()
        let metrics = await quantityMetrics + sleep
        return ExportPayload(generatedAt: iso.string(from: Date()),
                             workouts: await workouts,
                             metrics: metrics)
    }

    private func collectWorkouts() async -> [WorkoutSample] {
        let samples = await runAnchored(type: HKWorkoutType.workoutType(), anchorKey: "workouts")
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
        return out
    }

    private func collectQuantityMetrics() async -> [MetricSample] {
        var out: [MetricSample] = []
        for spec in metricSpecs {
            let samples = await runAnchored(type: HKQuantityType(spec.id), anchorKey: spec.type)
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
        return out
    }

    /// Aggregate asleep category samples into one `sleep_hours` metric per local night.
    private func collectSleep() async -> [MetricSample] {
        let samples = await runAnchored(type: HKCategoryType(.sleepAnalysis), anchorKey: "sleep")
        let asleep: Set<Int> = [
            HKCategoryValueSleepAnalysis.asleepUnspecified.rawValue,
            HKCategoryValueSleepAnalysis.asleepCore.rawValue,
            HKCategoryValueSleepAnalysis.asleepDeep.rawValue,
            HKCategoryValueSleepAnalysis.asleepREM.rawValue,
        ]
        var perNight: [String: (seconds: Double, start: Date, end: Date)] = [:]
        let cal = Calendar.current
        for case let s as HKCategorySample in samples where asleep.contains(s.value) {
            let dayKey = ISO8601DateFormatter.dateOnly.string(from: s.startDate)
            let dur = s.endDate.timeIntervalSince(s.startDate)
            if var night = perNight[dayKey] {
                night.seconds += dur
                night.start = min(night.start, s.startDate)
                night.end = max(night.end, s.endDate)
                perNight[dayKey] = night
            } else {
                perNight[dayKey] = (dur, s.startDate, s.endDate)
            }
        }
        _ = cal
        return perNight.values.map { night in
            MetricSample(type: "sleep_hours",
                         value: (night.seconds / 3600).rounded(toPlaces: 2),
                         unit: "h",
                         start: iso.string(from: night.start),
                         end: iso.string(from: night.end))
        }
    }

    // MARK: Anchored query helper

    private func runAnchored(type: HKSampleType, anchorKey: String) async -> [HKSample] {
        await withCheckedContinuation { continuation in
            let query = HKAnchoredObjectQuery(
                type: type,
                predicate: nil,
                anchor: AnchorStore.load(anchorKey),
                limit: HKObjectQueryNoLimit
            ) { _, samples, _, newAnchor, error in
                if let error { print("HealthBridge anchored query (\(anchorKey)) error: \(error)") }
                AnchorStore.save(anchorKey, newAnchor)
                continuation.resume(returning: samples ?? [])
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
