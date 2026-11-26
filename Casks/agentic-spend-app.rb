cask "agentic-spend-app" do
  version "1.8.2"
  sha256 "38747c280d35de7b40d6b26ad2fb69f84a690de54b0ef8cdcef17786de3848f4"

  url "https://github.com/tjorourke/agentic-spend/releases/download/v#{version}/Agentic-Spend-#{version}.zip"
  name "Agentic Spend"
  desc "Menu bar app for the Agentic Spend local AI spend dashboard"
  homepage "https://github.com/tjorourke/agentic-spend"

  depends_on macos: :ventura
  depends_on formula: "tjorourke/agentic-spend/agentic-spend"

  app "Agentic Spend.app"

  uninstall quit: "com.agenticspend.menubar"

  zap trash: [
    "~/Library/Preferences/com.agenticspend.menubar.plist",
    "~/Library/Saved Application State/com.agenticspend.menubar.savedState",
  ]

  caveats <<~EOS
    Start it:  open "/Applications/Agentic Spend.app"
    Dashboard: http://127.0.0.1:8787 (also from the menu bar icon > Open dashboard)

    Agentic Spend.app is signed ad hoc and is not notarised by Apple, so macOS
    blocks its first launch, and again after each upgrade. To approve it:
      1. Open Agentic Spend from Applications. When macOS says it can't check
         it, click Done.
      2. System Settings > Privacy & Security > Open Anyway (in the Security
         section, next to "Agentic Spend was blocked").
      3. Click Open Anyway again and enter your password or use Touch ID.

    If the icon still does not appear, macOS is hiding it:
      System Settings > Menu Bar > Allow in the Menu Bar > Agentic Spend
    (switch it on, or off and on again). The app now stays open and shows these
    steps when that happens.

    It reads usage through the agentic-spend formula, which bundles the
    ccusage binary (MIT; notice in THIRD_PARTY_NOTICES.md in the formula's libexec).
  EOS
end
