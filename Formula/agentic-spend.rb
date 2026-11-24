class AgenticSpend < Formula
  desc "Local spend dashboard for Claude Code, Grok, Codex, OpenCode etc."
  homepage "https://github.com/tjorourke/agentic-spend"
  url "https://github.com/tjorourke/agentic-spend/archive/refs/tags/v1.2.1.tar.gz"
  sha256 "51cef52f2ebcfbe7c1da656e763aefa1f88376c7e3ad571a25322567facd3bcc"
  version "1.2.1"

  license "Apache-2.0"

  depends_on :macos
  depends_on "node"

  def install
    libexec.install Dir["*"] - ["Formula"]
    (bin/"agentic-spend").write <<~SH
      #!/bin/bash
      set -euo pipefail
      DATA_DIR="${AGENTIC_SPEND_DATA_DIR:-$HOME/.agentic-spend}"
      mkdir -p "$DATA_DIR"
      PORT=8787
      URL="http://127.0.0.1:${PORT}/"
      PIDFILE="$DATA_DIR/.server.pid"
      is_running() { [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }
      if is_running; then
        echo "agentic-spend: server already running (pid $(cat "$PIDFILE")) at $URL"
      else
        echo "agentic-spend: starting server at $URL"
        nohup python3 "#{opt_libexec}/server.py" >"$DATA_DIR/.server.log" 2>&1 &
        echo $! > "$PIDFILE"
        sleep 0.6
      fi
      open "$URL" 2>/dev/null || echo "Open $URL in your browser."
    SH
    chmod 0755, bin/"agentic-spend"
  end

  test do
    system "python3", "-c",
           "import sys; sys.path.insert(0, '#{libexec}'); import server, account_usage, provider_usage, statusline_setup, data_dir, token_sources"
  end
end
