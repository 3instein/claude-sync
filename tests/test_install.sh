#!/usr/bin/env bash
# Acceptance test for install.sh. Runs it twice against a fake HOME and
# checks it is idempotent and never drops existing settings.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTALL_SH="$REPO_ROOT/install.sh"

FAKE_HOME="$(mktemp -d)"
cleanup() { rm -rf "$FAKE_HOME"; }
trap cleanup EXIT

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

mkdir -p "$FAKE_HOME/.claude"
cat > "$FAKE_HOME/.claude/settings.json" <<'JSON'
{
  "otherTopLevelKey": "keep-me",
  "permissions": {
    "allow": [
      "Bash(ls:*)"
    ]
  }
}
JSON

# Never point install.sh at a real HOME: this is a throwaway temp dir.
HOME="$FAKE_HOME" bash "$INSTALL_SH" >/dev/null
HOME="$FAKE_HOME" bash "$INSTALL_SH" >/dev/null

[ -L "$FAKE_HOME/.local/bin/claude-sync" ] || fail "missing claude-sync symlink"
[ "$(readlink "$FAKE_HOME/.local/bin/claude-sync")" = "$REPO_ROOT/claude-sync" ] \
  || fail "claude-sync symlink points at the wrong target"

[ -L "$FAKE_HOME/.claude/skills/claude-sync" ] || fail "missing skill symlink"
[ "$(readlink "$FAKE_HOME/.claude/skills/claude-sync")" = "$REPO_ROOT/skill" ] \
  || fail "skill symlink points at the wrong target"

CACHE_DIR="$FAKE_HOME/.cache/claude-sync"
[ -d "$CACHE_DIR" ] || fail "missing cache dir"
MODE="$(stat -c '%a' "$CACHE_DIR" 2>/dev/null || stat -f '%Lp' "$CACHE_DIR")"
[ "$MODE" = "700" ] || fail "cache dir mode is $MODE, want 700"

python3 - "$FAKE_HOME/.claude/settings.json" <<'PYEOF'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as f:
    data = json.load(f)

allow = data.get("permissions", {}).get("allow", [])
count = allow.count("Bash(claude-sync status:*)")
assert count == 1, f"expected the status rule exactly once, found {count}"
assert "Bash(ls:*)" in allow, "the existing allow rule was dropped"
assert data.get("otherTopLevelKey") == "keep-me", "the existing top-level key was dropped"
PYEOF

echo "OK: install.sh is idempotent and keeps existing settings"
