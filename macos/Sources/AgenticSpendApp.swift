import AppKit
import SwiftUI
import WebKit

@main
struct AgenticSpendApp: App {
    @StateObject private var store = Store()

    var body: some Scene {
        MenuBarExtra {
            MenuContent().environmentObject(store)
        } label: {
            MenuBarLabel().environmentObject(store)
        }
        .menuBarExtraStyle(.menu)
    }
}

struct MenuContent: View {
    @EnvironmentObject var store: Store

    var body: some View {
        if let s = store.summary {
            if s.plans.isEmpty {
                Text("No plan readings yet")
            }
            ForEach(s.plans) { plan in
                Button("\(plan.label ?? plan.key)   \(Fmt.pct(plan.pct))") {
                    store.openDashboard(path: "?tab=accounts")
                }
                if let sess = plan.session, sess.percent != nil || sess.note != nil {
                    let note = sess.note.map { " · \($0)" } ?? ""
                    Text("Session (5-hour)  \(Fmt.pct(sess.percent))\(note)")
                }
                if let week = plan.week, week.percent != nil || week.note != nil {
                    let note = week.note.map { " · \($0)" } ?? ""
                    Text("Weekly (7-day)  \(Fmt.pct(week.percent))\(note)")
                }
                if let pace = plan.sessionPace {
                    Text((pace.warn == true ? "⚠︎ " : "") + Fmt.paceLine(pace))
                }
                let line = Fmt.forecastLine(plan)
                if !line.isEmpty && line != plan.note {
                    Text(line)
                } else if !line.isEmpty && plan.session == nil && plan.week == nil {
                    Text(line)
                }
            }
            Divider()
            Text("Today: \(Fmt.money(s.today?.listUsd)) list price · \(Fmt.tokens(s.today?.tokens)) tokens")
        }
        if !store.status.isEmpty {
            Text(store.status)
        }
        if store.serverDown && store.launcherPath == nil {
            Button("How to install the server…") { store.openInstallHelp() }
        }
        Divider()
        Button("Open dashboard") { store.openDashboard() }
            .keyboardShortcut("d")
        Button("Open in browser") { store.openInBrowser() }
        Button("Refresh") { Task { await store.refresh() } }
            .keyboardShortcut("r")
        Menu("Menu bar shows") {
            Picker("Menu bar shows", selection: $store.display) {
                ForEach(MenuBarDisplay.allCases) { mode in
                    Text(mode.title).tag(mode)
                }
            }
            .pickerStyle(.inline)
            .labelsHidden()
        }
        Toggle("Start at login", isOn: Binding(
            get: { store.loginEnabled },
            set: { store.setLogin($0) }
        ))
        if !store.notificationsAllowed {
            Text("Notifications are off for this app")
        }
        Divider()
        Button("Quit Agentic Spend") { NSApp.terminate(nil) }
            .keyboardShortcut("q")
    }
}

/// The full dashboard in a native window (WKWebView on the local server).
@MainActor
enum DashboardWindow {
    private static var window: NSWindow?
    private static var webView: WKWebView?

    static func show(url: URL) {
        if window == nil {
            let wv = WKWebView(frame: NSRect(x: 0, y: 0, width: 1320, height: 880))
            let w = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 1320, height: 880),
                styleMask: [.titled, .closable, .miniaturizable, .resizable],
                backing: .buffered, defer: false)
            w.title = "Agentic Spend"
            w.contentView = wv
            w.isReleasedWhenClosed = false
            w.setFrameAutosaveName("AgenticSpendDashboard")
            w.center()
            window = w
            webView = wv
        }
        webView?.load(URLRequest(url: url))
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }
}
