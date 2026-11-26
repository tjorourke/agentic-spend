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

struct PlanWindow: Decodable {
    let percent: Double?
    let resetsAt: String?
    let note: String?
    let state: String?
    let asOf: String?
}

/// Pace in the current Claude 5-hour window (server: pacing.py).
struct SessionPace: Decodable {
    let currentPct: Double?
    let ratePerHour: Double?
    let projectedPct: Double?
    let hitsLimitAt: String?
    let resetAt: String?
    let basis: String?
    let warn: Bool?
    let idle: Bool?
    let label: String?
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
    let note: String?
    let session: PlanWindow?
    let week: PlanWindow?
    let sessionPace: SessionPace?
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
    let paceWarning: SessionPace?
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

    /// Same wording as the dashboard's forecast line. A reset window uses
    /// the server's note ("reset Fri 15:00, no use since") instead of a
    /// forecast, because there is nothing to project from.
    static func forecastLine(_ plan: Plan) -> String {
        if plan.week?.state == "reset" || (plan.forecast == nil && !(plan.note ?? "").isEmpty) {
            return plan.note ?? ""
        }
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

    /// "Session pace: would hit 100% around 16:40, before the 18:00 reset".
    static func paceLine(_ p: SessionPace) -> String {
        if (p.currentPct ?? 0) >= 100 { return "Session (5-hour) limit reached, resets \(when(p.resetAt))" }
        if p.warn == true, let hit = p.hitsLimitAt {
            return "Session pace: would hit 100% around \(when(hit)), before the \(when(p.resetAt)) reset"
        }
        if p.idle == true { return "Session (5-hour): no Claude use in the last 30 minutes" }
        return "Session pace: on track for \(pct(min(100, p.projectedPct ?? 0))) by \(when(p.resetAt))"
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
