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

/// The menu bar draws template images and text in its own colour, so the
/// amber warning state is a pre-rendered, non-template image instead.
enum AmberLabel {
    static let amber = NSColor.systemOrange

    static func image(title: String, display: MenuBarDisplay) -> NSImage {
        let font = NSFont.menuBarFont(ofSize: 0)
        let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: amber]
        let text = display == .iconOnly ? "" : title
        let textSize = (text as NSString).size(withAttributes: attrs)
        let showIcon = display != .percentOnly
        let iconSize: CGFloat = showIcon ? 15 : 0
        let gap: CGFloat = (showIcon && !text.isEmpty) ? 4 : 0
        let height: CGFloat = 18
        let size = NSSize(width: ceil(iconSize + gap + textSize.width), height: height)
        let img = NSImage(size: size, flipped: false) { _ in
            if showIcon, let base = NSImage(systemSymbolName: "exclamationmark.triangle.fill", accessibilityDescription: "Session limit warning") {
                let cfg = NSImage.SymbolConfiguration(pointSize: 12, weight: .semibold)
                    .applying(NSImage.SymbolConfiguration(paletteColors: [amber]))
                let sym = base.withSymbolConfiguration(cfg) ?? base
                sym.draw(in: NSRect(x: 0, y: (height - iconSize) / 2, width: iconSize, height: iconSize))
            }
            if !text.isEmpty {
                (text as NSString).draw(at: NSPoint(x: iconSize + gap, y: (height - textSize.height) / 2), withAttributes: attrs)
            }
            return true
        }
        img.isTemplate = false
        return img
    }
}

struct MenuBarLabel: View {
    @EnvironmentObject var store: Store

    var body: some View {
        if store.paceWarning {
            Image(nsImage: AmberLabel.image(title: store.menuTitle, display: store.display))
                .accessibilityLabel("\(store.menuTitle), Claude session limit on pace to run out")
        } else {
            normal
        }
    }

    @ViewBuilder private var normal: some View {
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
