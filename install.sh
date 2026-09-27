#!/usr/bin/env bash
# Install claude-sync: the CLI symlink, the skill symlink, the cache
# folder, and the "status" permission rule. Safe to run more than once.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ensure_dir() {
  local dir="$1"
  if [ ! -d "$dir" ]; then
    mkdir -p "$dir"
    echo "created $dir"
  fi
}

ensure_link() {
  local target="$1" link="$2"
  if [ -L "$link" ] && [ "$(readlink "$link")" = "$target" ]; then
    return
  fi
  ln -sfn "$target" "$link"
  echo "linked $link -> $target"
}

dir_mode() {
  stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"
}

ensure_dir "$HOME/.local/bin"
ensure_link "$REPO/claude-sync" "$HOME/.local/bin/claude-sync"

ensure_dir "$HOME/.claude/skills"
ensure_link "$REPO/skill" "$HOME/.claude/skills/claude-sync"

ensure_dir "$HOME/.cache/claude-sync"
if [ "$(dir_mode "$HOME/.cache/claude-sync")" != "700" ]; then
  chmod 700 "$HOME/.cache/claude-sync"
  echo "set mode 700 on $HOME/.cache/claude-sync"
fi

# Add the one permission rule claude-sync needs, without touching any
# other key or rule already in settings.json.
python3 - "$HOME/.claude/settings.json" <<'PYEOF'
import json
import os
import sys
import tempfile

path = sys.argv[1]
rule = "Bash(claude-sync status:*)"

existed = os.path.exists(path)
data = {}
mode = 0o600
if existed:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    mode = os.stat(path).st_mode & 0o777

permissions = data.setdefault("permissions", {})
allow = permissions.setdefault("allow", [])

if rule in allow:
    sys.exit(0)

allow.append(rule)

dir_name = os.path.dirname(path) or "."
os.makedirs(dir_name, exist_ok=True)
fd, tmp_path = tempfile.mkstemp(dir=dir_name)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.chmod(tmp_path, mode)
    os.replace(tmp_path, path)
except BaseException:
    os.unlink(tmp_path)
    raise

print(f"added permission rule {rule} to {path}")
PYEOF
