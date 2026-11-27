class AgenticSpend < Formula
  desc "Local spend dashboard for Claude Code, Grok, Codex, OpenCode etc."
  homepage "https://github.com/tjorourke/agentic-spend"
  url "https://github.com/tjorourke/agentic-spend/archive/refs/tags/v1.8.4.tar.gz"
  sha256 "13b1cf4e4dbdb44e2740a8963d671146cd0be793c7e31a1ed16c8da7a85c1466"

  license "Apache-2.0"

  depends_on :macos

  # ccusage 20 is a native binary. We bundle the build for this Mac from its
  # npm packages (MIT, see THIRD_PARTY_NOTICES.md), so no Node.js is needed.
  resource "ccusage" do
    on_arm do
      url "https://registry.npmjs.org/@ccusage/ccusage-darwin-arm64/-/ccusage-darwin-arm64-20.1.2.tgz"
      sha256 "042f5da67678cc232bb45b4a5f81eb3370e8373d53f7d7bcf07759a0e5accac8"
    end
    on_intel do
      url "https://registry.npmjs.org/@ccusage/ccusage-darwin-x64/-/ccusage-darwin-x64-20.1.2.tgz"
      sha256 "1bfa504ee2fc21eb2fcffc5b80915915af0c366a6109e4c37d7b770c9a4e4728"
    end
  end

  def install
    libexec.install Dir["*"] - ["Formula", "Casks", "macos"]
    resource("ccusage").stage do
      (libexec/"vendor").install "bin/ccusage"
    end
    chmod 0755, libexec/"vendor/ccusage"
    (bin/"agentic-spend").write <<~SH
      #!/bin/bash
      set -euo pipefail
      DATA_DIR="${AGENTIC_SPEND_DATA_DIR:-$HOME/.agentic-spend}"
      mkdir -p "$DATA_DIR"
      PORT="${AGENTIC_SPEND_PORT:-8787}"
      URL="http://127.0.0.1:${PORT}/"
      PIDFILE="$DATA_DIR/.server.pid"
      LIBEXEC="#{opt_libexec}"
      PY="$(command -v python3 || echo /usr/bin/python3)"
      is_running() { [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }
      usage() {
        echo "usage: agentic-spend [start|--no-open|serve|stop|status|login on|off|status]"
        echo "  start      start the server if needed and open the dashboard (default)"
        echo "  --no-open  start the server if needed, do not open a browser"
        echo "  serve      run the server in the foreground (used by the login item)"
        echo "  stop       stop the server started by this launcher"
        echo "  status     say whether the server is running"
        echo "  login      on, off or status: start the server at login (LaunchAgent)"
      }
      cmd="${1:-start}"
      case "$cmd" in
        serve)
          if is_running; then
            echo "agentic-spend: server already running (pid $(cat "$PIDFILE"))"
            exit 0
          fi
          echo $$ > "$PIDFILE"
          exec "$PY" "$LIBEXEC/server.py"
          ;;
        login)
          exec "$PY" "$LIBEXEC/login_item.py" "${2:-status}"
          ;;
        stop)
          if is_running; then
            kill "$(cat "$PIDFILE")" && rm -f "$PIDFILE"
            echo "agentic-spend: stopped"
          else
            echo "agentic-spend: not running"
          fi
          ;;
        status)
          if is_running; then echo "agentic-spend: running (pid $(cat "$PIDFILE")) at $URL"; else echo "agentic-spend: not running"; fi
          ;;
        start|open|--no-open)
          if is_running; then
            echo "agentic-spend: server already running (pid $(cat "$PIDFILE")) at $URL"
          else
            echo "agentic-spend: starting server at $URL"
            # This command opens the browser itself below, so the server's
            # open-once-on-first-run would give a second tab.
            if [[ "$cmd" != "--no-open" && -z "${AGENTIC_SPEND_NO_OPEN:-}" ]]; then
              export AGENTIC_SPEND_NO_FIRST_RUN_OPEN=1
            fi
            nohup "$PY" "$LIBEXEC/server.py" >"$DATA_DIR/.server.log" 2>&1 &
            echo $! > "$PIDFILE"
            sleep 0.6
          fi
          if [[ "$cmd" != "--no-open" && -z "${AGENTIC_SPEND_NO_OPEN:-}" ]]; then
            open "$URL" 2>/dev/null || echo "Open $URL in your browser."
          fi
          ;;
        -h|--help|help)
          usage
          ;;
        *)
          usage >&2
          exit 2
          ;;
      esac
    SH
    chmod 0755, bin/"agentic-spend"
  end

  def caveats
    <<~EOS
      Dashboard: http://127.0.0.1:8787
        agentic-spend            start the server and open the dashboard
        agentic-spend login on   start it at login (the first run opens the dashboard once;
                                 set AGENTIC_SPEND_NO_FIRST_RUN_OPEN=1 to skip that)

      Menu bar app (optional):
        brew install --cask tjorourke/agentic-spend/agentic-spend-app
        open "/Applications/Agentic Spend.app"

      The menu bar app is signed ad hoc and not notarised by Apple. macOS blocks its
      first launch, and again after each upgrade, until you approve it:
        System Settings > Privacy & Security > Open Anyway
      If its icon still does not appear: System Settings > Menu Bar > Allow in the
      Menu Bar > Agentic Spend (switch it on, or off and on again).

      If Homebrew stops with "untrusted tap" (newer Homebrew versions), trust the
      tap once and run the command again:
        brew trust tjorourke/agentic-spend
    EOS
  end

  test do
    modules = %w[server account_usage provider_usage statusline_setup data_dir token_sources grok_usage
                 grokbot_usage source_detection insights notify digest login_item ccusage_bin
                 ccusage_sources seat_prices pacing model_mix plan_prices plan_detection pricing_region
                 opencode_backends desktop_sources allowance_history usage_archive]
    system "python3", "-c",
           "import sys; sys.path.insert(0, '#{libexec}'); import #{modules.join(", ")}"
    assert_match "usage: agentic-spend", shell_output("#{bin}/agentic-spend --help")
    assert_match "ccusage 20.1.2", shell_output("#{libexec}/vendor/ccusage --version")
    assert_path_exists libexec/"THIRD_PARTY_NOTICES.md"
    system "python3", "-c",
           "import sys; sys.path.insert(0, '#{libexec}'); import ccusage_bin; " \
           "r = ccusage_bin.resolve(env={}); assert r['source'] == 'bundled', r"
  end
end
