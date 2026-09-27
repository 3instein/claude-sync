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


def _load(path):
    try:
        with open(path, "rb") as f:
            return json.loads(f.read())
    except (OSError, ValueError):
        return {}


def _marketplace_source(entry):
    src = entry.get("source") if isinstance(entry, dict) else None
    if not isinstance(src, dict):
        return None
    if src.get("source") == "github" and src.get("repo"):
        return src["repo"]
    return src.get("url") or src.get("path")


def cmd_plugins_read(args, stdin):
    plugins_dir = f"{args['home']}/.claude/plugins"
    installed_obj = _load(f"{plugins_dir}/installed_plugins.json")
    plugins = installed_obj.get("plugins", {}) if isinstance(installed_obj, dict) else {}
    installed = sorted(pid for pid, records in plugins.items()
                       if isinstance(records, list)
                       and any(isinstance(r, dict) and r.get("scope") == "user" for r in records))
    mkt_obj = _load(f"{plugins_dir}/known_marketplaces.json")
    marketplaces = {}
    if isinstance(mkt_obj, dict):
        for name, entry in mkt_obj.items():
            source = _marketplace_source(entry)
            if source:
                marketplaces[name] = source
    return {"installed": installed, "marketplaces": marketplaces}


def _valid_argv(argv):
    """The four shapes the contract allows: install/uninstall X, marketplace add/remove X."""
    if len(argv) == 2 and argv[0] in ("install", "uninstall"):
        return True
    return len(argv) == 3 and argv[0] == "marketplace" and argv[1] in ("add", "remove")


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
