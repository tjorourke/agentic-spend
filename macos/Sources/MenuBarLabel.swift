import AppKit
import SwiftUI

/// What the menu bar item shows: the gauge icon, the highest plan percentage,
/// or both. Saved in UserDefaults so it survives restarts.
enum MenuBarDisplay: String, CaseIterable, Identifiable {
    case iconAndPercent
    case iconOnly
    case percentOnly

    var id: String { rawValue }

    var title: String {
        switch self {
        case .iconAndPercent: return "Icon and percentage"
        case .iconOnly: return "Icon only"
        case .percentOnly: return "Percentage only"
        }
    }

    static let defaultsKey = "menuBarDisplay"

    static func load(_ defaults: UserDefaults = .standard) -> MenuBarDisplay {
        defaults.string(forKey: defaultsKey).flatMap(MenuBarDisplay.init(rawValue:)) ?? .iconAndPercent
    }

    func save(_ defaults: UserDefaults = .standard) {
        defaults.set(rawValue, forKey: Self.defaultsKey)
    }
}

enum MenuBarIcon {
    /// A template SF Symbol (macOS tints it to match the menu bar, light or
    /// dark). Falls back to an older symbol name on early macOS 13 builds.
    static let image: NSImage = {
        let names = ["gauge.with.dots.needle.50percent", "speedometer"]
        let config = NSImage.SymbolConfiguration(pointSize: 13, weight: .medium)
        for name in names {
            if let base = NSImage(systemSymbolName: name, accessibilityDescription: "Agentic Spend"),
               let img = base.withSymbolConfiguration(config) {
                img.isTemplate = true
                return img
            }
        }
        let empty = NSImage(size: NSSize(width: 14, height: 14))
        empty.isTemplate = true
        return empty
    }()
}

struct MenuBarLabel: View {
    @EnvironmentObject var store: Store

    var body: some View {
        switch store.display {
        case .iconOnly:
            Image(nsImage: MenuBarIcon.image)
                .accessibilityLabel(store.menuTitle)
        case .percentOnly:
            Text(store.menuTitle)
        case .iconAndPercent:
            HStack(spacing: 4) {
                Image(nsImage: MenuBarIcon.image)
                Text(store.menuTitle)
            }
        }
    }
}
