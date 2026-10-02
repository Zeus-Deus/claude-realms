#!/usr/bin/env bash
# Drive an interactive Claude Code session with the plugin loaded, inside the
# test container, through tmux. Local slash commands need no model, so a dummy
# API key is enough for the mod's UI; model turns need real auth (CLAUDE_CODE_OAUTH_TOKEN).
#   tui.sh start            start claude in tmux session "cc"
#   tui.sh send TEXT...     type text and press Enter
#   tui.sh keys KEY...      send raw tmux keys
#   tui.sh show [-e]        print the screen (-e keeps colours)
set -euo pipefail
case "${1:-}" in
  start)
    mkdir -p ~/proj && cd ~/proj
    key="${ANTHROPIC_API_KEY:-sk-ant-api03-dummy-key-for-local-ui-tests-only-0000000000000000AA}"
    python3 - "$key" <<'PY'
import json, os, sys
path = os.path.expanduser("~/.claude.json")
data = json.load(open(path)) if os.path.exists(path) else {}
data.update(hasCompletedOnboarding=True, theme="dark")
data.setdefault("projects", {})[os.path.expanduser("~/proj")] = {"hasTrustDialogAccepted": True, "allowedTools": []}
data["customApiKeyResponses"] = {"approved": [sys.argv[1][-20:]], "rejected": []}
json.dump(data, open(path, "w"))
PY
    printf 'set -g default-terminal "tmux-256color"\nset -as terminal-features ",*:RGB"\nset -as terminal-overrides ",*:Tc"\nset -g history-limit 5000\n' > ~/.tmux.conf
    tmux -f ~/.tmux.conf new-session -d -s cc -x "${COLS:-200}" -y "${ROWS:-60}" \
      "env ANTHROPIC_API_KEY=$key DISABLE_AUTOUPDATER=1 COLORTERM=truecolor claude --plugin-dir /plugin ${CLAUDE_ARGS:-} ; sleep 600"
    ;;
  send) shift; tmux send-keys -t cc -l "$*"; sleep 0.3; tmux send-keys -t cc Enter ;;
  keys) shift; tmux send-keys -t cc "$@" ;;
  show) shift; tmux capture-pane -t cc -p ${1:-} ;;
  png) shift; tmux capture-pane -t cc -p -e > /tmp/screen.ansi; python3 "$(dirname "$0")/ansi2png.py" /tmp/screen.ansi "${1:-/tmp/screen.png}" ;;
esac
