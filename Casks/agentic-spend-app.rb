cask "agentic-spend-app" do
  version "1.7.1"
  sha256 "216665ff5dcbc59c0d50a0a363b850896fd8074edf2502952188de35983575ad"

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
    Agentic Spend.app is signed ad hoc and is not notarised by Apple, so macOS
    blocks the first launch. To approve it once:
      1. Open Agentic Spend from Applications. When macOS says it can't check
         it, click Done.
      2. Open System Settings > Privacy & Security, scroll to Security and click
         Open Anyway next to "Agentic Spend was blocked".
      3. Click Open Anyway again and enter your password or use Touch ID.
    After that it opens normally.

    It reads usage through the agentic-spend formula, which bundles the
    ccusage binary (MIT; notice in THIRD_PARTY_NOTICES.md in the formula's libexec).
  EOS
end
