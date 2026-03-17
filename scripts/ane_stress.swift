#!/usr/bin/env swift

import CoreML
import Foundation

private let defaultModelPath = "resources/models/tiny_ane_stress.mlmodel"

enum ScriptError: LocalizedError {
    case invalidArgument(String)
    case unsupportedModel(String)
    case runtime(String)

    var errorDescription: String? {
        switch self {
        case .invalidArgument(let message):
            return message
        case .unsupportedModel(let message):
            return message
        case .runtime(let message):
            return message
        }
    }
}

struct Options {
    var seconds = 20
    var workers = 2
    var computeUnits = "cpu-and-ne"
    var modelPath = defaultModelPath
    var help = false

    static func parse(arguments: [String]) throws -> Options {
        var options = Options()
        var index = 1

        func requireValue(for flag: String) throws -> String {
            index += 1
            guard index < arguments.count else {
                throw ScriptError.invalidArgument("Missing value for \(flag)")
            }
            return arguments[index]
        }

        while index < arguments.count {
            let argument = arguments[index]
            switch argument {
            case "-h", "--help":
                options.help = true
            case "--seconds":
                let value = try requireValue(for: argument)
                guard let parsed = Int(value), parsed > 0 else {
                    throw ScriptError.invalidArgument("--seconds expects a positive integer")
                }
                options.seconds = parsed
            case "--workers":
                let value = try requireValue(for: argument)
                guard let parsed = Int(value), parsed > 0 else {
                    throw ScriptError.invalidArgument("--workers expects a positive integer")
                }
                options.workers = parsed
            case "--compute-units":
                let value = try requireValue(for: argument)
                let supported = ["all", "cpu-and-ne", "cpu-only"]
                guard supported.contains(value) else {
                    throw ScriptError.invalidArgument(
                        "--compute-units must be one of: \(supported.joined(separator: ", "))"
                    )
                }
                options.computeUnits = value
            case "--model-path":
                options.modelPath = try requireValue(for: argument)
            default:
                throw ScriptError.invalidArgument("Unknown argument: \(argument)")
            }
            index += 1
        }

        return options
    }

    static func usage() -> String {
        """
        ANE stress helper for metop.

        This script loads a tiny bundled Core ML model and loops inference to
        create sustained accelerator load without downloading a large external
        model.

        Usage:
          swift scripts/ane_stress.swift [options]

        Options:
          --seconds N           Run for N seconds (default: 20)
          --workers N           Concurrent inference workers (default: 2)
          --compute-units MODE  all | cpu-and-ne | cpu-only (default: cpu-and-ne)
          --model-path PATH     Use a different local .mlmodel or .mlpackage
          -h, --help            Show this help

        Example:
          swift scripts/ane_stress.swift --seconds 30 --workers 4

        Tip:
          In another terminal, run `sudo metop -i 500` to watch ANE activity.
        """
    }
}

final class SharedStats {
    private let lock = NSLock()
    private var predictions = 0
    private var failures = 0
    private var errors: [String] = []

    func recordPrediction() {
        lock.lock()
        predictions += 1
        lock.unlock()
    }

    func recordFailure(_ message: String) {
        lock.lock()
        failures += 1
        if errors.count < 5 {
            errors.append(message)
        }
        lock.unlock()
    }

    func snapshot() -> (predictions: Int, failures: Int, errors: [String]) {
        lock.lock()
        defer { lock.unlock() }
        return (predictions, failures, errors)
    }
}

func resolveComputeUnits(_ value: String) throws -> MLComputeUnits {
    switch value {
    case "all":
        return .all
    case "cpu-only":
        return .cpuOnly
    case "cpu-and-ne":
        return .cpuAndNeuralEngine
    default:
        throw ScriptError.invalidArgument("Unsupported compute unit mode: \(value)")
    }
}

func compileModel(at sourceURL: URL) throws -> URL {
    print("Compiling model: \(sourceURL.path)")
    return try MLModel.compileModel(at: sourceURL)
}

func makeMultiArrayInput(name: String, description: MLFeatureDescription) throws -> MLDictionaryFeatureProvider {
    guard let constraint = description.multiArrayConstraint else {
        throw ScriptError.unsupportedModel("Model input \(name) has no multi-array constraint")
    }

    let shape = constraint.shape
    guard !shape.isEmpty else {
        throw ScriptError.unsupportedModel("Model input \(name) does not expose a concrete shape")
    }

    let array = try MLMultiArray(shape: shape, dataType: constraint.dataType)
    let count = array.count

    switch constraint.dataType {
    case .double:
        let pointer = array.dataPointer.bindMemory(to: Double.self, capacity: count)
        for index in 0..<count {
            pointer[index] = Double((index % 17) - 8) / 8.0
        }
    case .float32:
        let pointer = array.dataPointer.bindMemory(to: Float32.self, capacity: count)
        for index in 0..<count {
            pointer[index] = Float32((index % 17) - 8) / 8.0
        }
    case .float16:
        let pointer = array.dataPointer.bindMemory(to: Float16.self, capacity: count)
        for index in 0..<count {
            pointer[index] = Float16((index % 17) - 8) / 8.0
        }
    case .int32:
        let pointer = array.dataPointer.bindMemory(to: Int32.self, capacity: count)
        for index in 0..<count {
            pointer[index] = Int32(index % 7)
        }
    @unknown default:
        throw ScriptError.unsupportedModel("Unsupported MLMultiArray type for input \(name)")
    }

    return try MLDictionaryFeatureProvider(dictionary: [name: MLFeatureValue(multiArray: array)])
}

func makeInputProvider(for model: MLModel) throws -> MLFeatureProvider {
    let inputs = model.modelDescription.inputDescriptionsByName.sorted { $0.key < $1.key }
    guard inputs.count == 1, let (name, description) = inputs.first else {
        throw ScriptError.unsupportedModel("This helper currently supports models with exactly one input")
    }

    switch description.type {
    case .multiArray:
        return try makeMultiArrayInput(name: name, description: description)
    default:
        throw ScriptError.unsupportedModel("Unsupported input type for \(name): \(description.type.rawValue)")
    }
}

func runWorker(
    id: Int,
    compiledModelURL: URL,
    configuration: MLModelConfiguration,
    deadline: Date,
    stats: SharedStats
) {
    do {
        let model = try MLModel(contentsOf: compiledModelURL, configuration: configuration)
        let input = try makeInputProvider(for: model)

        _ = try model.prediction(from: input)

        while Date() < deadline {
            _ = try model.prediction(from: input)
            stats.recordPrediction()
        }
    } catch {
        stats.recordFailure("worker \(id): \(error.localizedDescription)")
    }
}

do {
    let options = try Options.parse(arguments: CommandLine.arguments)
    if options.help {
        print(Options.usage())
        exit(0)
    }

    let sourceModelURL = URL(fileURLWithPath: options.modelPath)
    guard FileManager.default.fileExists(atPath: sourceModelURL.path) else {
        throw ScriptError.runtime("Model file not found: \(sourceModelURL.path)")
    }

    let compiledModelURL = try compileModel(at: sourceModelURL)

    let configuration = MLModelConfiguration()
    configuration.computeUnits = try resolveComputeUnits(options.computeUnits)

    let start = Date()
    let deadline = start.addingTimeInterval(TimeInterval(options.seconds))
    let stats = SharedStats()
    let group = DispatchGroup()

    print("Model source: \(sourceModelURL.path)")
    print("Compute units: \(options.computeUnits)")
    print("Workers: \(options.workers)")
    print("Duration: \(options.seconds)s")
    print("Start another terminal with: sudo metop -i 500")

    for workerID in 0..<options.workers {
        group.enter()
        DispatchQueue.global(qos: .userInitiated).async {
            defer { group.leave() }
            runWorker(
                id: workerID,
                compiledModelURL: compiledModelURL,
                configuration: configuration,
                deadline: deadline,
                stats: stats
            )
        }
    }

    var lastPredictionCount = 0
    while group.wait(timeout: .now() + 1) == .timedOut {
        let snapshot = stats.snapshot()
        let delta = snapshot.predictions - lastPredictionCount
        lastPredictionCount = snapshot.predictions
        let elapsed = Int(Date().timeIntervalSince(start))
        print("[\(elapsed)s] predictions=\(snapshot.predictions) last_1s=\(delta) failures=\(snapshot.failures)")
    }

    let final = stats.snapshot()
    let elapsed = max(Date().timeIntervalSince(start), 0.001)
    let throughput = Double(final.predictions) / elapsed

    print("Finished.")
    print(String(format: "Predictions: %d in %.1fs (%.1f/s)", final.predictions, elapsed, throughput))

    if !final.errors.isEmpty {
        print("Errors:")
        for message in final.errors {
            print("  - \(message)")
        }
    }

    if final.predictions == 0 {
        throw ScriptError.runtime("No successful inferences completed")
    }
} catch {
    fputs("ane_stress.swift: \(error.localizedDescription)\n", stderr)
    exit(1)
}
