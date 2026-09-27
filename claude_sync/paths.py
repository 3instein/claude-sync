"""Machine-neutral paths, keys, path fields and hashes. See docs/contract.md."""
import fnmatch
import json
import re

try:
    from claude_sync.model import Machine, HOME_TOKEN, DESKTOP_TOKEN
except ImportError:  # inside the combined helper program
    pass


# ---- folder_name ----

def _java_hash_code(s: str) -> int:
    """Java's String.hashCode: h = 31*h + ord(c), 32-bit signed overflow."""
    h = 0
    for ch in s:
        h = (31 * h + ord(ch)) & 0xFFFFFFFF
    return h - 0x100000000 if h >= 0x80000000 else h


def _base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if n == 0:
        return "0"
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(digits[r])
    return "".join(reversed(out))


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", s)


def folder_name(cwd: str) -> str:
    """Claude Code's project folder name for cwd, with the 200-character cut and hash."""
    slug = _slug(cwd)
    if len(slug) > 200:
        slug = slug[:200] + "-" + _base36(abs(_java_hash_code(cwd)))
    return slug


# ---- neutral paths ----

def neutral(path: str, m: "Machine") -> str:
    """Absolute path on m to neutral form. Paths outside home stay the same."""
    if path == m.desktop or path.startswith(m.desktop + "/"):
        return DESKTOP_TOKEN + path[len(m.desktop):]
    if path == m.home or path.startswith(m.home + "/"):
        return HOME_TOKEN + path[len(m.home):]
    return path


def localize(npath: str, m: "Machine") -> str:
    """Neutral path to an absolute path on m."""
    if npath == DESKTOP_TOKEN or npath.startswith(DESKTOP_TOKEN + "/"):
        return m.desktop + npath[len(DESKTOP_TOKEN):]
    if npath == HOME_TOKEN or npath.startswith(HOME_TOKEN + "/"):
        return m.home + npath[len(HOME_TOKEN):]
    return npath


# ---- slug form of the prefixes, for the "[...]" key when cwd is unknown ----

def _neutral_slug(folder: str, m: "Machine") -> str:
    for prefix, token in ((_slug(m.desktop), DESKTOP_TOKEN), (_slug(m.home), HOME_TOKEN)):
        if folder == prefix or folder.startswith(prefix + "-"):
            return token + folder[len(prefix):]
    return folder


def _localize_slug(slug: str, m: "Machine") -> str:
    for token, prefix in ((DESKTOP_TOKEN, _slug(m.desktop)), (HOME_TOKEN, _slug(m.home))):
        if slug == token or slug.startswith(token + "-"):
            return prefix + slug[len(token):]
    return slug


# ---- keys ----

def key_for(root: str, rel: str, m: "Machine", folder_cwd: str | None) -> str | None:
    """Key for the file at <root dir of m>/<rel>. folder_cwd is the absolute cwd of the
    project folder when rel is under projects/. None when no key is possible."""
    if root == "cli" and rel.startswith("projects/"):
        folder, _, rest = rel[len("projects/"):].partition("/")
        if folder_cwd is not None:
            token = "{" + neutral(folder_cwd, m) + "}"
        elif len(folder) <= 200:
            token = "[" + _neutral_slug(folder, m) + "]"
        else:
            return None
        return f"cli/projects/{token}/{rest}"
    return f"{root}/{rel}"


def _split_token(remainder: str) -> tuple[str, str]:
    """Split "<{...} or [...]>/<rest>" into the token and the rest."""
    end_char = "}" if remainder.startswith("{") else "]"
    end = remainder.index(end_char)
    token, rest = remainder[:end + 1], remainder[end + 1:]
    if rest.startswith("/"):
        rest = rest[1:]
    return token, rest


def _resolve_folder(token: str, m: "Machine") -> str:
    if token.startswith("{"):
        return folder_name(localize(token[1:-1], m))
    return _localize_slug(token[1:-1], m)


def key_to_path(key: str, m: "Machine") -> str:
    """Absolute path on m for key."""
    root, rest = key.split("/", 1)
    base = m.home + "/.claude" if root == "cli" else m.desktop
    if root == "cli" and rest.startswith("projects/"):
        token, path_rest = _split_token(rest[len("projects/"):])
        folder = _resolve_folder(token, m)
        return f"{base}/projects/{folder}/{path_rest}" if path_rest else f"{base}/projects/{folder}"
    return f"{base}/{rest}"


# ---- file kinds ----

def classify(key: str) -> str:
    """transcript | session | json | raw"""
    if key.startswith("cli/projects/") and key.endswith(".jsonl"):
        return "transcript"
    if fnmatch.fnmatch(key, "desktop/claude-code-sessions/*/*/local_*.json"):
        return "session"
    if key == "cli/settings.json" or fnmatch.fnmatch(key, "desktop/claude-code-sessions/*/*/archived-sessions.idx"):
        return "json"
    return "raw"


def is_exec(key: str) -> bool:
    if key == "cli/settings.json":
        return True
    return key.startswith(("cli/skills/", "cli/agents/", "cli/commands/"))


def transcript_cwd(data: bytes) -> str | None:
    """cwd of the first transcript line that has one."""
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("cwd"), str):
            return obj["cwd"]
    return None


# ---- transcript line fields ----

def _map_transcript_fields(obj: dict, fn):
    """Apply fn to every path field in one transcript line. Returns (new_obj, found)."""
    obj = dict(obj)
    found = False

    if isinstance(obj.get("cwd"), str):
        obj["cwd"] = fn(obj["cwd"])
        found = True

    scc = obj.get("serverClassifierContext")
    if isinstance(scc, dict) and isinstance(scc.get("context"), dict):
        ctx = dict(scc["context"])
        ctx_changed = False
        if isinstance(ctx.get("live_cwd"), str):
            ctx["live_cwd"] = fn(ctx["live_cwd"])
            ctx_changed = True
        gs = ctx.get("git_state")
        if isinstance(gs, dict) and isinstance(gs.get("cwd"), str):
            gs = dict(gs)
            gs["cwd"] = fn(gs["cwd"])
            ctx["git_state"] = gs
            ctx_changed = True
        if ctx_changed:
            scc = dict(scc)
            scc["context"] = ctx
            obj["serverClassifierContext"] = scc
            found = True

    att = obj.get("attachment")
    if isinstance(att, dict) and isinstance(att.get("snapshot"), dict) \
            and isinstance(att["snapshot"].get("workingDirectory"), str):
        snap = dict(att["snapshot"])
        snap["workingDirectory"] = fn(snap["workingDirectory"])
        att = dict(att)
        att["snapshot"] = snap
        obj["attachment"] = att
        found = True

    if isinstance(obj.get("trackingPath"), str):
        obj["trackingPath"] = fn(obj["trackingPath"])
        found = True

    backup = obj.get("backup")
    if isinstance(backup, dict) and isinstance(backup.get("realParentDir"), str):
        backup = dict(backup)
        backup["realParentDir"] = fn(backup["realParentDir"])
        obj["backup"] = backup
        found = True

    snap_top = obj.get("snapshot")
    if isinstance(snap_top, dict) and isinstance(snap_top.get("trackedFileBackups"), dict):
        new_tfb = {}
        for k, v in snap_top["trackedFileBackups"].items():
            new_k = fn(k) if k.startswith("/") else k
            if isinstance(v, dict) and isinstance(v.get("realParentDir"), str):
                v = dict(v)
                v["realParentDir"] = fn(v["realParentDir"])
            new_tfb[new_k] = v
        snap_top = dict(snap_top)
        snap_top["trackedFileBackups"] = new_tfb
        obj["snapshot"] = snap_top
        found = True

    return obj, found


def _rewrite_transcript(data: bytes, fn) -> bytes:
    """Re-serialize each transcript line whose path fields fn touches; keep other lines' bytes.

    For .jsonl input, data may lack a final newline (a partial last line); that line
    is kept as-is if it does not parse, so a cut-off tail never crashes this.
    """
    text = data.decode("utf-8", "surrogateescape")
    trailing = text.endswith("\n")
    lines = text.split("\n")
    if trailing:
        lines.pop()
    out = []
    for raw in lines:
        if raw == "":
            out.append(raw)
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            out.append(raw)
            continue
        if not isinstance(obj, dict):
            out.append(raw)
            continue
        new_obj, found = _map_transcript_fields(obj, fn)
        out.append(json.dumps(new_obj, ensure_ascii=False, separators=(",", ":")) if found else raw)
    result = "\n".join(out)
    if trailing:
        result += "\n"
    return result.encode("utf-8", "surrogateescape")


# ---- session fields ----

def _map_session(obj: dict, fn) -> dict:
    obj = dict(obj)
    if isinstance(obj.get("cwd"), str):
        obj["cwd"] = fn(obj["cwd"])
    if isinstance(obj.get("originCwd"), str):
        obj["originCwd"] = fn(obj["originCwd"])
    if isinstance(obj.get("scratchPromptRecents"), list):
        obj["scratchPromptRecents"] = [fn(x) if isinstance(x, str) else x
                                        for x in obj["scratchPromptRecents"]]
    return obj


# ---- settings.json: prefix occurs anywhere in any string, at a path boundary ----

def _scan_replace(text: str, old: str, new: str) -> str:
    if not old:
        return text
    # Both ends at a boundary: "/Users/r" must not match inside "/x/Users/r" or "/Users/r2".
    return re.sub(r"(?<![\w.~/-])" + re.escape(old) + r"(?=/|\Z|[^\w.-])", new, text)


def _neutral_text(text: str, m: "Machine") -> str:
    text = _scan_replace(text, m.desktop, DESKTOP_TOKEN)
    return _scan_replace(text, m.home, HOME_TOKEN)


def _localize_text(text: str, m: "Machine") -> str:
    text = _scan_replace(text, DESKTOP_TOKEN, m.desktop)
    return _scan_replace(text, HOME_TOKEN, m.home)


def _map_settings(obj, fn):
    if isinstance(obj, dict):
        return {k: _map_settings(v, fn) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_map_settings(v, fn) for v in obj]
    if isinstance(obj, str):
        return fn(obj)
    return obj


# ---- hashes and localization ----

def normalize_bytes(key: str, data: bytes, m: "Machine") -> bytes:
    """Neutral form of the content, the input of FileInfo.hash."""
    kind = classify(key)
    if kind == "raw":
        return data
    if kind == "transcript":
        return _rewrite_transcript(data, lambda p: neutral(p, m))
    obj = json.loads(data)
    if kind == "session":
        obj = _map_session(obj, lambda p: neutral(p, m))
    elif key == "cli/settings.json":
        obj = _map_settings(obj, lambda s: _neutral_text(s, m))
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def localize_bytes(key: str, data: bytes, src: "Machine", dst: "Machine") -> bytes:
    """Content from src written for dst: only path fields change."""
    kind = classify(key)
    if kind == "raw":
        return data
    if kind == "transcript":
        return _rewrite_transcript(data, lambda p: localize(neutral(p, src), dst))
    if kind == "session":
        obj = _map_session(json.loads(data), lambda p: localize(neutral(p, src), dst))
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if key == "cli/settings.json":
        obj = _map_settings(json.loads(data), lambda s: _localize_text(_neutral_text(s, src), dst))
        # settings.json is edited by hand, so it keeps the indented form Claude Code writes.
        return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return data  # json kind other than settings.json: no path fields to change
