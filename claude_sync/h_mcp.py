"""Helper commands for the mcp part: read and write the mcp-related keys of ~/.claude.json,
and resolve a command on this machine. Runs on Python 3.9. See docs/contract.md, Phase 2 parts, mcp."""
import json
import os

try:
    from claude_sync.helper import _backup, _atomic_write, _mkdirs_secure, _load_manifest, COMMANDS
except ImportError:  # inside the combined helper program
    pass

PROJECT_FIELDS = ("allowedTools", "hasTrustDialogAccepted", "enabledMcpjsonServers", "disabledMcpjsonServers")
CLAUDE_JSON_KEY = "home/.claude.json"  # manifest key for backup/undo; not a sync key of paths.py


def _claude_json_path(home):
    return f"{home}/.claude.json"


def _mcp_cache_root(home):
    # named apart from helper._cache_root(m): that one takes a Machine, this takes home directly,
    # and both live in the same namespace once remote.program() concatenates the two files.
    return f"{home}/.cache/claude-sync"


def cmd_mcp_read(args, stdin):
    p = _claude_json_path(args["home"])
    if not os.path.isfile(p):
        return {"mtime": None, "size": None, "mcpServers": {}, "projects": {}}
    with open(p, "rb") as f:
        data = f.read()
    st = os.stat(p)
    obj = json.loads(data) if data else {}
    projects = {}
    for path, proj in (obj.get("projects") or {}).items():
        if isinstance(proj, dict):
            sub = {k: proj[k] for k in PROJECT_FIELDS if k in proj}
            if sub:
                projects[path] = sub
    return {"mtime": int(st.st_mtime), "size": len(data),
            "mcpServers": obj.get("mcpServers") or {}, "projects": projects}


def cmd_mcp_write(args, stdin):
    """Change only mcpServers and the four project fields, keep every other key
    and key order, skip when the file changed since the plan read it."""
    home = args["home"]
    p = _claude_json_path(home)
    expect = args.get("expect")
    exists = os.path.isfile(p)
    if expect is None:
        if exists:
            return {"skipped": True}
        data = b"{}"
    else:
        if not exists:
            return {"skipped": True}
        st = os.stat(p)
        if [int(st.st_mtime), st.st_size] != list(expect):
            return {"skipped": True}
        with open(p, "rb") as f:
            data = f.read()
    obj = json.loads(data) if data else {}
    obj["mcpServers"] = args.get("mcpServers", {})
    projects = obj.setdefault("projects", {})
    for path, fields in args.get("projects", {}).items():
        proj = projects.setdefault(path, {})
        for k in PROJECT_FIELDS:
            if k in fields:
                proj[k] = fields[k]
            else:
                proj.pop(k, None)
    root = _mcp_cache_root(home)
    run_id = args["run_id"]
    backup_dir = f"{root}/backup/{run_id}"
    _mkdirs_secure(backup_dir, root)
    manifest = _load_manifest(backup_dir)
    _backup(p, CLAUDE_JSON_KEY, backup_dir, root, manifest)
    new_data = (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode()
    _atomic_write(p, new_data, mode=0o600)
    manifest[CLAUDE_JSON_KEY]["written"] = [int(os.stat(p).st_mtime), len(new_data)]
    _atomic_write(f"{backup_dir}/manifest.json", json.dumps(manifest).encode())
    return {"ok": True}


def _search_dirs(home):
    # PATH first (a non-interactive ssh shell has a short one), then the usual install spots.
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    extra = [f"{home}/.local/bin", "/opt/homebrew/bin", "/usr/local/bin"]
    seen, out = set(), []
    for d in path_dirs + extra:
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def cmd_which(args, stdin):
    """For each name, the first matching executable in the search dirs, or null.
    For each dir, whether it exists (used to check a project's folder on this side)."""
    search = _search_dirs(args["home"])
    names = {}
    for name in args.get("names", []):
        found = None
        for d in search:
            cand = os.path.join(d, name)
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                found = cand
                break
        names[name] = found
    exist = {d: os.path.isdir(d) for d in args.get("dirs", [])}
    return {"names": names, "dirs": exist}


COMMANDS.update({
    "mcp_read": cmd_mcp_read,
    "mcp_write": cmd_mcp_write,
    "which": cmd_which,
})
