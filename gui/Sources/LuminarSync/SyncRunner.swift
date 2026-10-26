import Foundation

/// Runs luminar_sync.py as a separate process; all the sync logic lives there.
struct SyncRunner {
    let python: String
    let script: URL

    struct Output {
        let stdout: Data
        let stderr: String
        let status: Int32
    }

    /// Where the script is: the LUMINARSYNC_SCRIPT environment variable (for development), otherwise the app bundle.
    static func locate() -> SyncRunner? {
        let env = ProcessInfo.processInfo.environment
        let python = env["LUMINARSYNC_PYTHON"] ?? "/usr/bin/python3"
        if let path = env["LUMINARSYNC_SCRIPT"] {
            return SyncRunner(python: python, script: URL(fileURLWithPath: path))
        }
        guard let url = Bundle.main.url(forResource: "luminar_sync", withExtension: "py") else { return nil }
        return SyncRunner(python: python, script: url)
    }

    func run(_ arguments: [String]) async throws -> Output {
        try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                let process = Process()
                process.executableURL = URL(fileURLWithPath: python)
                process.arguments = ["-B", script.path] + arguments
                var environment = ProcessInfo.processInfo.environment
                environment["PYTHONDONTWRITEBYTECODE"] = "1"
                environment["PYTHONIOENCODING"] = "utf-8"
                process.environment = environment
                let out = Pipe()
                let err = Pipe()
                process.standardOutput = out
                process.standardError = err
                do {
                    try process.run()
                } catch {
                    continuation.resume(throwing: error)
                    return
                }
                // both pipes must be read concurrently, otherwise a full pipe can block the process
                var errData = Data()
                let group = DispatchGroup()
                group.enter()
                DispatchQueue.global().async {
                    errData = err.fileHandleForReading.readDataToEndOfFile()
                    group.leave()
                }
                let outData = out.fileHandleForReading.readDataToEndOfFile()
                group.wait()
                process.waitUntilExit()
                continuation.resume(returning: Output(
                    stdout: outData,
                    stderr: String(decoding: errData, as: UTF8.self),
                    status: process.terminationStatus
                ))
            }
        }
    }
}
