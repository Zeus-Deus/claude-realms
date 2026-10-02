#!/usr/bin/env bash
# A real agent turn against the plugin, in the disposable test container:
# Claude launches an app in its realm, clicks it, and verifies the result.
#
#   test/e2e/agent.sh TOKEN_FILE      a token from `claude setup-token`
#   test/e2e/agent.sh --use-login      your current Claude Code sign-in
#
# A token goes into the container's environment only. --use-login mounts
# ~/.claude/.credentials.json read-only and copies it inside the container,
# whose filesystem is discarded afterwards; nothing is printed. If the access
# token expires mid-run, the container refreshes it, which can sign the host
# session out (log in again with /login).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
auth=${1:?usage: agent.sh TOKEN_FILE | --use-login}
# The real Claude Code binary (not a version-manager wrapper script).
claude_bin=${CLAUDE_BIN:-}
if [[ -z $claude_bin ]]; then
  for candidate in ~/.local/share/mise/installs/claude/latest/claude "$(command -v claude)"; do
    candidate=$(readlink -f "$candidate" 2>/dev/null || true)
    if [[ -f $candidate ]] && ! head -c 2 "$candidate" | grep -q '#!'; then claude_bin=$candidate; break; fi
  done
fi
[[ -n $claude_bin ]] || { echo "set CLAUDE_BIN to the claude executable" >&2; exit 1; }
artifacts="$root/test/artifacts"
mkdir -p "$artifacts"

PROMPT='Test a GUI app in your private realm desktop. Launch gtk3-demo in the realm, capture the realm desktop, then click the "Button Boxes" entry in its left sidebar, capture again and verify the change. Finally answer with exactly one line: RESULT: <the title now shown in the window header bar>.'

if [[ $auth == --use-login ]]; then
  auth_args=(--mount "$HOME/.claude/.credentials.json:/seed/credentials.json:ro")
  token=""
else
  auth_args=(--env CLAUDE_CODE_OAUTH_TOKEN)
  token=$(cat "$auth")
fi
seed=()
if docker volume inspect crr-drivers >/dev/null 2>&1; then seed=(--mount crr-drivers:/seed/drivers:ro); fi

env -i PATH=/usr/bin:/bin HOME="$HOME" CLAUDE_CODE_OAUTH_TOKEN="$token" \
  "$root/test/docker/run.sh" --mount "$claude_bin:/usr/local/bin/claude:ro" --mount "$artifacts:/artifacts" \
  "${auth_args[@]}" "${seed[@]}" -- bash -lc '
    set -e
    unset REALMS_HOME
    [ -n "$CLAUDE_CODE_OAUTH_TOKEN" ] || unset CLAUDE_CODE_OAUTH_TOKEN
    if [ -f /seed/credentials.json ]; then
      mkdir -p -m 700 ~/.claude && cp /seed/credentials.json ~/.claude/.credentials.json && chmod 600 ~/.claude/.credentials.json
    fi
    if [ -d /seed/drivers ]; then
      # A verified driver and a fresh release listing: no GitHub API calls needed.
      mkdir -p -m 700 ~/.claude/plugins/data/realms-inline && cp -a /seed/drivers ~/.claude/plugins/data/realms-inline/drivers
    fi
    mkdir -p ~/proj && cd ~/proj
    python3 -c "import json,os; p=os.path.expanduser(\"~/.claude.json\"); json.dump({\"hasCompletedOnboarding\": True}, open(p, \"w\"))"
    claude -p "$0" --plugin-dir /plugin --dangerously-skip-permissions \
      --output-format stream-json --verbose > /artifacts/agent-run.jsonl 2>/artifacts/agent-run.stderr || true
    python3 - <<PY
import json
tools, result = [], ""
for line in open("/artifacts/agent-run.jsonl"):
    try:
        event = json.loads(line)
    except ValueError:
        continue
    if event.get("type") == "assistant":
        for block in event["message"].get("content", []):
            if block.get("type") == "tool_use":
                tools.append(block["name"])
    if event.get("type") == "result":
        result = event.get("result", "")
print("tools used:", tools)
print("final:", result.strip().splitlines()[-1] if result.strip() else "(none)")
ok = any(t.endswith("realm_launch") for t in tools) and any(t.endswith("__click") for t in tools) \
    and "Button Boxes" in result
print("AGENT E2E", "PASS" if ok else "FAIL")
PY
  ' "$PROMPT"
