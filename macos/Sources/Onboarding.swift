import AppKit
import SwiftUI
import UserNotifications

/// Where macOS keeps the "Allow in the Menu Bar" switches (macOS 26) and the
/// Open Anyway button for apps that are not notarised.
enum SystemSettingsLinks {
    static let menuBar = URL(string: "x-apple.systempreferences:com.apple.ControlCenter-Settings.extension")!
    static let privacySecurity = URL(string: "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension")!
}

/// Words shown when macOS hides the menu bar item, and on first launch.
/// Kept in one place so the build self-test can check they are not empty.
enum OnboardingText {
    static let welcomeTitle = "Welcome to Agentic Spend"
    static let welcomeBody = """
    The dashboard runs at http://127.0.0.1:8787 and has opened in its own window.
    The menu bar shows your highest plan usage, for example "G 57%". Click it for the menu, \
    including Open dashboard.
    """
    static let hiddenTitle = "macOS is hiding the Agentic Spend icon"
    static let hiddenBody = """
    Agentic Spend is running, but macOS will not show its menu bar icon yet.
    1. System Settings > Privacy & Security: scroll to Security and click Open Anyway next to \
    "Agentic Spend". This is needed once per version, because the app is not notarised by Apple.
    2. System Settings > Menu Bar: under Allow in the Menu Bar, switch Agentic Spend on. \
    If it already shows as on, switch it off and on again.
    Then click Show icon again. The dashboard keeps working at http://127.0.0.1:8787 in the meantime.
    """
}

/// One small native window for the first-run welcome and the hidden-icon help.
@MainActor
enum InfoWindow {
    private static var window: NSWindow?

    enum Kind { case welcome, hidden }

    static func show(_ kind: Kind, store: Store) {
        let view = InfoView(kind: kind).environmentObject(store)
        let host = NSHostingController(rootView: view)
        if window == nil {
            let w = NSWindow(contentViewController: host)
            w.styleMask = [.titled, .closable]
            w.isReleasedWhenClosed = false
            window = w
        } else {
            window?.contentViewController = host
        }
        window?.title = kind == .welcome ? OnboardingText.welcomeTitle : "Agentic Spend"
        window?.center()
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }

    static func close() { window?.close() }
}

struct InfoView: View {
    let kind: InfoWindow.Kind
    @EnvironmentObject var store: Store

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(kind == .welcome ? OnboardingText.welcomeTitle : OnboardingText.hiddenTitle)
                .font(.headline)
            Text(kind == .welcome ? OnboardingText.welcomeBody : OnboardingText.hiddenBody)
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                if kind == .hidden {
                    Button("Open Privacy & Security") { NSWorkspace.shared.open(SystemSettingsLinks.privacySecurity) }
                    Button("Open Menu Bar settings") { NSWorkspace.shared.open(SystemSettingsLinks.menuBar) }
                    Button("Show icon again") { store.showMenuBarItem() }
                }
                Spacer()
                Button("Open dashboard") { store.openDashboard() }
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(20)
        .frame(width: 680)
    }
}

/// Reopening the app (Finder, Spotlight, `open`) brings the icon back, or
/// explains why macOS is still hiding it.
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        Task { @MainActor in Store.shared?.reopened() }
        return true
    }
}

/// `AgenticSpend --self-test`: run by macos/build.sh on every build. Fails the
/// build if the menu bar label would render empty or zero width, or if the
/// bundle is missing what makes it a menu bar app.
@MainActor
enum SelfTest {
    static func run() -> Int32 {
        var failures: [String] = []
        let icon = MenuBarIcon.image
        if MenuBarIcon.isFallback { failures.append("menu bar SF Symbol did not load (empty fallback image)") }
        if icon.size.width <= 0 || icon.size.height <= 0 { failures.append("menu bar icon has zero size") }
        if !icon.isTemplate { failures.append("menu bar icon is not a template image") }
        for title in ["AS", "AS !", "G 57%"] {
            let w = (title as NSString).size(withAttributes: [.font: NSFont.menuBarFont(ofSize: 0)]).width
            if w <= 0 { failures.append("label \"\(title)\" measures zero width") }
        }
        for display in MenuBarDisplay.allCases {
            if AmberLabel.image(title: "G 57%", display: display).size.width <= 0 {
                failures.append("warning label is zero width for \(display.rawValue)")
            }
        }
        if Store.placeholderTitle.isEmpty { failures.append("placeholder menu title is empty") }
        for text in [OnboardingText.welcomeBody, OnboardingText.hiddenBody] where text.isEmpty {
            failures.append("onboarding text is empty")
        }
        let info = Bundle.main.infoDictionary ?? [:]
        if Bundle.main.bundlePath.hasSuffix(".app") {
            if info["LSUIElement"] as? Bool != true { failures.append("Info.plist LSUIElement is not true") }
            if info["CFBundleIdentifier"] as? String != "com.agenticspend.menubar" {
                failures.append("unexpected bundle identifier \(info["CFBundleIdentifier"] ?? "nil")")
            }
        }
        if failures.isEmpty {
            print("self-test ok: icon \(Int(icon.size.width))x\(Int(icon.size.height)) template, labels non-empty")
            return 0
        }
        for f in failures { FileHandle.standardError.write(Data("self-test FAILED: \(f)\n".utf8)) }
        return 1
    }
}
