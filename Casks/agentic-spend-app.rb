cask "agentic-spend-app" do
  version "1.5.0"
  sha256 "fbf9b3e37e014643f1c23b0473b3a99db63f13d52410989fdb32f46d910f0225"

  url "https://github.com/tjorourke/agentic-spend/releases/download/v#{version}/Agentic-Spend-#{version}.zip"
  name "Agentic Spend"
  desc "Menu bar app for the Agentic Spend local AI spend dashboard"
  homepage "https://github.com/tjorourke/agentic-spend"

  depends_on macos: ">= :ventura"
  depends_on formula: "tjorourke/agentic-spend/agentic-spend"

  app "Agentic Spend.app"

  uninstall quit: "com.agenticspend.menubar"

  zap trash: [
    "~/Library/Preferences/com.agenticspend.menubar.plist",
    "~/Library/Saved Application State/com.agenticspend.menubar.savedState",
  ]

  caveats <<~EOS
    Agentic Spend.app is signed ad hoc and is not notarised by Apple, so macOS
    blocks the first launch. To approve it once:
      1. Open Agentic Spend from Applications. When macOS says it can't check
         it, click Done.
      2. Open System Settings > Privacy & Security, scroll to Security and click
         Open Anyway next to "Agentic Spend was blocked".
      3. Click Open Anyway again and enter your password or use Touch ID.
    After that it opens normally.
  EOS
end
