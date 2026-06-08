import Foundation

/// Delivers an ExportPayload to LifeOS. Primary mode: authenticated POST over
/// Tailscale. (A file-write mode can be added for the synced-folder fallback.)
enum Uploader {
    static func post(_ payload: ExportPayload, to urlString: String, token: String) async throws -> String {
        guard let url = URL(string: urlString) else {
            throw err("Invalid server URL")
        }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.timeoutInterval = 60
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")

        let encoder = JSONEncoder()
        req.httpBody = try encoder.encode(payload)

        let (data, resp) = try await URLSession.shared.data(for: req)
        guard let http = resp as? HTTPURLResponse else { throw err("No HTTP response") }
        let body = String(data: data, encoding: .utf8) ?? ""
        guard (200..<300).contains(http.statusCode) else {
            throw err("HTTP \(http.statusCode): \(body)")
        }
        return body
    }

    private static func err(_ msg: String) -> NSError {
        NSError(domain: "HealthBridge.Uploader", code: 1,
                userInfo: [NSLocalizedDescriptionKey: msg])
    }
}
