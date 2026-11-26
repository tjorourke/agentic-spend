import AppKit
import Foundation
import ServiceManagement
import UserNotifications

/// Talks to the local Agentic Spend server, keeps the menu up to date, sends
/// the heartbeat that tells the server to route notifications here, and
/// shows those notifications natively.
@MainActor
final class Store: NSObject, ObservableObject, UNUserNotificationCenterDelegate {
    static let base = URL(string: "http://127.0.0.1:8787/")!
    static let installURL = URL(string: "https://github.com/tjorourke/agentic-spend#install")!
    static let placeholderTitle = "AS"
    static let welcomeKey = "welcomeShown"
    /// For the app delegate's reopen handler.
    static weak var shared: Store?

    @Published var summary: Summary?
    @Published var status: String = "Loading…"
    @Published var serverDown = false
    @Published var loginEnabled = false
    @Published var loginNeedsApproval = false
    @Published var notificationsAllowed = false
    @Published var display: MenuBarDisplay = MenuBarDisplay.load() {
        didSet { display.save() }
    }
    /// Bound to MenuBarExtra(isInserted:). SwiftUI sets it to false when macOS
    /// hides the item; the app then stays running and explains what to do.
    @Published var menuBarInserted = true {
        didSet {
            // SwiftUI flips this inside a view update; react on the next turn of
            // the run loop so nothing is published from within that update.
            if oldValue && !menuBarInserted && !reinserting {
                DispatchQueue.main.async { [weak self] in self?.menuBarHidden() }
            }
        }
    }
    let launcherPath: String?
    private var hiddenNoticeSent = false
    private var reinserting = false

    private var timer: Timer?
    private var startAttempted = false
    private var refreshing = false

    override init() {
        launcherPath = Store.findLauncher()
        super.init()
        readLoginState()
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
            Task { @MainActor in
                self.notificationsAllowed = granted
                if granted { await self.heartbeat() }
            }
        }
        Store.shared = self
        Task { await refresh() }
        if !UserDefaults.standard.bool(forKey: Store.welcomeKey) {
            UserDefaults.standard.set(true, forKey: Store.welcomeKey)
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                self.openDashboard()
                // If macOS is already hiding the icon, the help matters more.
                InfoWindow.show(self.menuBarInserted ? .welcome : .hidden, store: self)
            }
        }
        timer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.tick() }
        }
    }

    /// True when a Claude 5-hour window is on pace to run out before it resets.
    var paceWarning: Bool { summary?.paceWarning?.warn == true }

    var menuTitle: String {
        guard let top = summary?.plans.first, let pct = top.pct else {
            return serverDown ? "\(Store.placeholderTitle) !" : Store.placeholderTitle
        }
        return "\(Fmt.shortName(top)) \(Int(pct.rounded()))%"
    }

    // MARK: server

    static func findLauncher() -> String? {
        let candidates = ["/opt/homebrew/bin/agentic-spend", "/usr/local/bin/agentic-spend"]
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    private func startServer(_ path: String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: path)
        p.arguments = ["--no-open"]
        var env = ProcessInfo.processInfo.environment
        let brewBin = (path as NSString).deletingLastPathComponent
        env["PATH"] = [brewBin, "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", env["PATH"] ?? ""].joined(separator: ":")
        env["AGENTIC_SPEND_NO_OPEN"] = "1"
        p.environment = env
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        try? p.run()
    }

    private func tick() async {
        if notificationsAllowed { await heartbeat() }
        await refresh()
        readLoginState()
    }

    func refresh() async {
        if refreshing { return }
        refreshing = true
        var req = URLRequest(url: Store.base.appendingPathComponent("api/summary"))
        req.timeoutInterval = 120  // the first ccusage run after a restart can be slow
        do {
            let (data, resp) = try await URLSession.shared.data(for: req)
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            serverDown = false
            if code == 200 {
                summary = try JSONDecoder().decode(Summary.self, from: data)
                status = (summary?.stale ?? false) ? "Showing the last good reading" : ""
            } else {
                let err = try? JSONDecoder().decode(ServerError.self, from: data)
                status = "Server error: \(err?.error ?? "HTTP \(code)")"
            }
            refreshing = false
        } catch {
            refreshing = false
            serverDown = true
            if !startAttempted, let path = launcherPath {
                startAttempted = true
                status = "Starting the server…"
                startServer(path)
                try? await Task.sleep(nanoseconds: 3_000_000_000)
                await refresh()
                return
            }
            status = launcherPath == nil
                ? "Server not installed. Run: brew install tjorourke/agentic-spend/agentic-spend"
                : "Can't reach the server on 127.0.0.1:8787"
        }
    }

    private func heartbeat() async {
        var req = URLRequest(url: Store.base.appendingPathComponent("api/app/heartbeat"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = Data("{}".utf8)
        req.timeoutInterval = 10
        guard let (data, _) = try? await URLSession.shared.data(for: req),
              let hb = try? JSONDecoder().decode(HeartbeatResponse.self, from: data) else { return }
        for note in hb.pending ?? [] { show(note) }
    }

    private func show(_ note: PendingNotification) {
        let content = UNMutableNotificationContent()
        content.title = note.title
        content.body = note.message
        content.sound = .default
        content.userInfo = ["url": note.url ?? Store.base.absoluteString]
        let req = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(req)
    }

    // MARK: menu bar visibility

    private func menuBarHidden() {
        NSLog("Agentic Spend: macOS hid the menu bar item; staying open and showing help")
        InfoWindow.show(.hidden, store: self)
        guard !hiddenNoticeSent, notificationsAllowed else { return }
        hiddenNoticeSent = true
        let content = UNMutableNotificationContent()
        content.title = OnboardingText.hiddenTitle
        content.body = "Open System Settings > Privacy & Security (Open Anyway), then Menu Bar > Allow in the Menu Bar."
        content.userInfo = ["url": Store.base.absoluteString]
        UNUserNotificationCenter.current().add(
            UNNotificationRequest(identifier: "menubar-hidden", content: content, trigger: nil))
    }

    /// Ask macOS to show the item again; if it is still blocked, SwiftUI
    /// flips the binding back to false and the help window reappears.
    func showMenuBarItem() {
        InfoWindow.close()
        reinserting = true
        menuBarInserted = false
        DispatchQueue.main.async {
            self.menuBarInserted = true
            self.reinserting = false
        }
    }

    func reopened() {
        if menuBarInserted {
            openDashboard()
        } else {
            showMenuBarItem()
        }
    }

    // MARK: actions

    func openDashboard(path: String = "") {
        let url = URL(string: path, relativeTo: Store.base)?.absoluteURL ?? Store.base
        DashboardWindow.show(url: url)
    }

    func openInBrowser() {
        NSWorkspace.shared.open(Store.base)
    }

    func openInstallHelp() {
        NSWorkspace.shared.open(Store.installURL)
    }

    private func readLoginState() {
        let s = SMAppService.mainApp.status
        loginEnabled = (s == .enabled)
        loginNeedsApproval = (s == .requiresApproval)
    }

    func setLogin(_ on: Bool) {
        do {
            if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
        } catch {
            status = "Start at login: \(error.localizedDescription)"
        }
        // Read the state back rather than trusting the call.
        readLoginState()
        if loginNeedsApproval {
            status = "Approve Agentic Spend in System Settings > General > Login Items"
            SMAppService.openSystemSettingsLoginItems()
        }
    }

    // MARK: UNUserNotificationCenterDelegate

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter,
                                            willPresent notification: UNNotification,
                                            withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter,
                                            didReceive response: UNNotificationResponse,
                                            withCompletionHandler completionHandler: @escaping () -> Void) {
        let raw = response.notification.request.content.userInfo["url"] as? String
        Task { @MainActor in
            if let raw = raw, let url = URL(string: raw), url.host == "127.0.0.1" {
                DashboardWindow.show(url: url)
            } else {
                DashboardWindow.show(url: Store.base)
            }
        }
        completionHandler()
    }
}
