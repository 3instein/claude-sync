"""Helper commands for the plugins part: reads user-scope plugins and marketplaces, and
runs the four allowed `claude plugin ...` commands. See docs/contract.md, the plugins row.
Python 3.9."""
import json
import os
import shutil
import subprocess

try:
    from claude_sync.helper import COMMANDS
except ImportError:  # inside the combined program COMMANDS already exists
    pass

RUN_TIMEOUT = 180
OUTPUT_CAP = 2000
KNOWN_VERSION = 2


def _load(path):
    """(data, error). data is {} with no error when the file simply doesn't exist yet
    (a fresh machine); error is set for a file that exists but can't be read or parsed,
    so the caller never mistakes "broken" for "empty" (a truncated file must not read as
    zero plugins installed, or a plan would uninstall everything)."""
    if not os.path.exists(path):
        return {}, None
    try:
        with open(path, "rb") as f:
            return json.loads(f.read()), None
    except (OSError, ValueError) as e:
        return {}, str(e)


def _marketplace_source(entry):
    src = entry.get("source") if isinstance(entry, dict) else None
    if not isinstance(src, dict):
        return None
    if src.get("source") == "github" and src.get("repo"):
        return src["repo"]
    return src.get("url") or src.get("path")


def cmd_plugins_read(args, stdin):
    plugins_dir = f"{args['home']}/.claude/plugins"
    installed_obj, error = _load(f"{plugins_dir}/installed_plugins.json")
    if error is None and installed_obj and installed_obj.get("version", KNOWN_VERSION) != KNOWN_VERSION:
        error = f"unknown installed_plugins.json version: {installed_obj.get('version')!r}"
    mkt_obj, mkt_error = _load(f"{plugins_dir}/known_marketplaces.json")
    error = error or mkt_error
    if error:
        return {"error": error}
    plugins = installed_obj.get("plugins", {}) if isinstance(installed_obj, dict) else {}
    installed = sorted(pid for pid, records in plugins.items()
                       if isinstance(records, list)
                       and any(isinstance(r, dict) and r.get("scope") == "user" for r in records))
    marketplaces = {}
    if isinstance(mkt_obj, dict):
        for name, entry in mkt_obj.items():
            source = _marketplace_source(entry)
            if source:
                marketplaces[name] = source
    return {"installed": installed, "marketplaces": marketplaces}


def _safe_value(v):
    """A clone URL, plugin id or marketplace source is content from the other machine: it
    must never be read as an option by the real CLI's argument parser."""
    return isinstance(v, str) and v != "" and not v.startswith("-")


def _valid_argv(argv):
    """The four shapes the contract allows, each with `--` before its untrusted value:
    install/uninstall -- X, marketplace add/remove -- X."""
    if len(argv) == 3 and argv[0] in ("install", "uninstall") and argv[1] == "--":
        return _safe_value(argv[2])
    if len(argv) == 4 and argv[0] == "marketplace" and argv[1] in ("add", "remove") and argv[2] == "--":
        return _safe_value(argv[3])
    return False


def _plugins_claude_bin(home):
    # ponytail: the fake home's .local/bin is checked first, so a test's fake `claude`
    # script always wins over a real one on PATH; real machines rarely have both.
    # Named apart from helper._find_claude_bin: both land in one combined program's
    # globals, and a same-named function here would replace that one there.
    candidate = f"{home}/.local/bin/claude"
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return shutil.which("claude")


def cmd_plugins_run(args, stdin):
    argv = args.get("argv")
    if not isinstance(argv, list) or not _valid_argv(argv):
        raise ValueError(f"unsupported plugin command: {argv!r}")
    claude_bin = _plugins_claude_bin(args["home"])
    if not claude_bin:
        return {"code": 1, "output": "claude binary not found"}
    try:
        res = subprocess.run([claude_bin, "plugin"] + argv, capture_output=True, text=True, timeout=RUN_TIMEOUT)
        code, output = res.returncode, res.stdout + res.stderr
    except subprocess.TimeoutExpired:
        code, output = 1, "claude plugin command timed out"
    return {"code": code, "output": output[-OUTPUT_CAP:]}


COMMANDS.update({"plugins_read": cmd_plugins_read, "plugins_run": cmd_plugins_run})
