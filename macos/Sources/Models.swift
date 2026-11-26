import Foundation

// Shapes returned by the local server's /api/summary and /api/app/heartbeat.
// Every field is optional so an older or newer server never breaks decoding.

struct Forecast: Decodable {
    let currentPct: Double?
    let projectedPct: Double?
    let resetAt: String?
    let hitsLimitAt: String?
    let confidence: String?
}

struct WindowCompare: Decodable {
    let deltaPct: Double?
    let previousPeakPct: Double?
}

struct Plan: Decodable, Identifiable {
    let key: String
    let tool: String?
    let label: String?
    let pct: Double?
    let resetAt: String?
    let windowSeconds: Int?
    let forecast: Forecast?
    let compare: WindowCompare?
    var id: String { key }
}

struct Today: Decodable {
    let date: String?
    let listUsd: Double?
    let tokens: Int?
}

struct Summary: Decodable {
    let generatedAt: String?
    let stale: Bool?
    let today: Today?
    let plans: [Plan]
    let dashboardUrl: String?
}

struct PendingNotification: Decodable {
    let title: String
    let message: String
    let url: String?
}

struct HeartbeatResponse: Decodable {
    let ok: Bool?
    let pending: [PendingNotification]?
}

struct ServerError: Decodable {
    let error: String?
}

enum Fmt {
    private static let isoPlain: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()
    private static let isoFrac: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    static func date(_ s: String?) -> Date? {
        guard let s = s else { return nil }
        return isoPlain.date(from: s) ?? isoFrac.date(from: s)
    }

    /// "Sun 09:06" within the next six days, "10 Dec 15:40" further out.
    static func when(_ s: String?) -> String {
        guard let d = date(s) else { return "?" }
        let f = DateFormatter()
        f.locale = Locale.current
        f.dateFormat = abs(d.timeIntervalSinceNow) > 6 * 86400 ? "d MMM HH:mm" : "EEE HH:mm"
        return f.string(from: d)
    }

    static func pct(_ p: Double?) -> String {
        guard let p = p else { return "–" }
        return "\(Int(p.rounded()))%"
    }

    static func money(_ v: Double?) -> String {
        String(format: "$%.2f", v ?? 0)
    }

    static func tokens(_ n: Int?) -> String {
        let v = Double(n ?? 0)
        if v >= 1e9 { return String(format: "%.2fB", v / 1e9) }
        if v >= 1e6 { return String(format: "%.1fM", v / 1e6) }
        if v >= 1e3 { return String(format: "%.1fk", v / 1e3) }
        return String(Int(v))
    }

    /// Same wording as the dashboard's forecast line.
    static func forecastLine(_ plan: Plan) -> String {
        var parts: [String] = []
        if let fc = plan.forecast {
            var text: String
            if (fc.currentPct ?? 0) >= 100 {
                text = "at the limit now"
            } else if let hit = fc.hitsLimitAt {
                text = "would hit 100% around \(when(hit))"
            } else {
                text = "on track for \(pct(fc.projectedPct)) by \(when(fc.resetAt))"
            }
            if fc.confidence == "low" { text += " (early estimate)" }
            parts.append(text)
        } else if let reset = plan.resetAt {
            parts.append("resets \(when(reset))")
        }
        if let c = plan.compare, let d = c.deltaPct, c.previousPeakPct != nil {
            let r = Int(d.rounded())
            let noun = plan.windowSeconds == 7 * 86400 ? "last week" : "last window"
            parts.append(r == 0 ? "same peak as \(noun)" : "\(r > 0 ? "+" : "-")\(abs(r)) pts vs \(noun)")
        }
        return parts.joined(separator: " · ")
    }

    static func shortName(_ plan: Plan) -> String {
        switch plan.tool {
        case "grok-bot": return "G"
        case "claude": return "C"
        case "codex": return "X"
        default: return String((plan.label ?? plan.key).prefix(1)).uppercased()
        }
    }
}
