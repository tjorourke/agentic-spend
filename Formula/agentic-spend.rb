class AgenticSpend < Formula
  desc "Local spend dashboard for Claude Code, Grok, Codex, OpenCode etc."
  homepage "https://github.com/tjorourke/agentic-spend"
  url "https://github.com/tjorourke/agentic-spend/archive/refs/tags/v1.4.0.tar.gz"
  sha256 "5442d9649d1965121785a621e33df463d436986f412cebff83aeb39cb2075856"
  version "1.4.0"

  license "Apache-2.0"

  depends_on :macos
  depends_on "node"

  def install
    libexec.install Dir["*"] - ["Formula", "Casks", "macos"]
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

  test do
    system "python3", "-c",
           "import sys; sys.path.insert(0, '#{libexec}'); import server, account_usage, provider_usage, statusline_setup, data_dir, token_sources, grok_usage, grokbot_usage, source_detection, insights, notify, digest, login_item"
    assert_match "usage: agentic-spend", shell_output("#{bin}/agentic-spend --help")
  end
end
