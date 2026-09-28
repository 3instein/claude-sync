"""Helper command for the update part: run `claude update` under the given home.
Joins the combined helper program after helper.py (Python 3.9)."""
import os
import subprocess

try:
    from claude_sync.helper import COMMANDS
except ImportError:  # inside the combined program the names already exist
    pass


def _update_claude_bin(home):
    # Only the native install under the given home: never a claude found elsewhere on PATH.
    path = os.path.join(home, ".local", "bin", "claude")
    return path if os.path.isfile(path) and os.access(path, os.X_OK) else None


def _update_version(binary):
    out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=20)
    return out.stdout.strip() or None


def cmd_claude_update(args, stdin):
    binary = _update_claude_bin(args["home"])
    if not binary:
        return {"found": False}
    before = _update_version(binary)
    try:
        out = subprocess.run([binary, "update"], capture_output=True, text=True, timeout=180)
        code, tail = out.returncode, (out.stdout + out.stderr)[-500:]
    except subprocess.TimeoutExpired:
        code, tail = None, "timed out"
    return {"found": True, "before": before, "after": _update_version(binary), "code": code, "output": tail}


COMMANDS.update({"claude_update": cmd_claude_update})
