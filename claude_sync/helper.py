"""Runs on each machine, local or over SSH as one program with paths.py. See docs/contract.md."""
import hashlib
import io
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time

try:
    from claude_sync.model import Machine, CLI_ITEMS, DESKTOP_ITEMS, SKIP_NAMES, SKIP_PATHS
    from claude_sync.paths import *  # noqa: F401,F403
except ImportError:  # inside the combined program the names already exist
    pass

CACHE_REL = ".cache/claude-sync"
LOCK_NAME = "lock.json"
STATE_NAME = "state.json"
EXPECT_MEMBER = ".claude-sync-expect.json"


def _cache_root(m):
    return f"{m.home}/{CACHE_REL}"


def _mkdirs_secure(path, base):
    """Create path under base and force mode 700 on base and every folder down to it,
    because a file that leaks into ~/.cache/claude-sync must not be world/group readable."""
    os.makedirs(path, exist_ok=True)
    os.chmod(base, 0o700)
    rel = os.path.relpath(path, base)
    cur = base
    if rel != ".":
        for part in rel.split(os.sep):
            cur = os.path.join(cur, part)
            os.chmod(cur, 0o700)


def _atomic_write(path, data, mtime=None, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        if mtime is not None:
            os.utime(tmp, (mtime, mtime))
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _exec_mode(bits):
    # rule 6: any execute bit on the source means the copy is written 700, else 600.
    return 0o700 if bits & 0o111 else 0o600


def _under_root(path, root):
    # catches a symlink escape that a string-only safe_key check cannot see.
    rp, rroot = os.path.realpath(path), os.path.realpath(root)
    return rp == rroot or rp.startswith(rroot + os.sep)


def _key_root_dir(key, m):
    return f"{m.home}/.claude" if key.startswith("cli/") else m.desktop


def _expect_mismatch(target, exp):
    """rule 5: skip when the target exists and differs from exp, or exists when exp is null."""
    exists = os.path.exists(target)
    if exp is None:
        return exists
    if not exists:
        return False
    st = os.stat(target)
    return [int(st.st_mtime), _full_line_size(target, st.st_size)] != list(exp)


def _full_line_size(path, size):
    """The inventory counts a transcript only up to its last full line, so compare the same way."""
    if not path.endswith(".jsonl"):
        return size
    with open(path, "rb") as f:
        f.seek(max(0, size - (1 << 20)))
        tail = f.read()
        if b"\n" not in tail and size > len(tail):
            f.seek(0)
            tail = f.read()
    cut = tail.rfind(b"\n")
    return size - len(tail) + cut + 1 if cut >= 0 else 0


# --- info -----------------------------------------------------------------------------

def _find_claude_bin():
    found = shutil.which("claude")
    if found:
        return found
    # a non-interactive ssh shell has a short PATH, so also look in the usual install spot
    candidate = os.path.expanduser("~/.local/bin/claude")
    return candidate if os.path.isfile(candidate) and os.access(candidate, os.X_OK) else None


def _pgrep(pattern, exact):
    try:
        res = subprocess.run(["pgrep", "-x" if exact else "-f", pattern], capture_output=True, text=True)
    except FileNotFoundError:
        return []
    if res.returncode != 0:
        return []
    return [int(p) for p in res.stdout.split()]


def cmd_info(args, stdin):
    home = os.path.expanduser("~")
    system = platform.system()
    desktop = (f"{home}/Library/Application Support/Claude" if system == "Darwin"
               else f"{home}/.config/Claude")
    claude_bin = _find_claude_bin()
    version = None
    if claude_bin:
        try:
            out = subprocess.run([claude_bin, "--version"], capture_output=True, text=True, timeout=5)
            version = out.stdout.strip() or None
        except Exception:
            version = None
    if system == "Darwin":
        app_running = bool(_pgrep("Claude", exact=True))
    else:
        app_running = bool(_pgrep("/usr/lib/claude-desktop/claude-desktop", exact=False))
    cli_pids = [p for p in _pgrep("claude", exact=True) if p != os.getpid()]
    return {
        "hostname": socket.gethostname(),
        "home": home,
        "desktop": desktop,
        "platform": system,
        "claude_version": version,
        "app_running": app_running,
        "cli_pids": cli_pids,
    }


# --- inventory --------------------------------------------------------------------------

def _walk_root(base_dir, items, root, warnings):
    """Yield keys' rel path (posix separators) for every regular file under the given
    phase-1 items. A missing item is not a warning (a new machine can lack agents/); a
    symlinked item, an unreadable folder, or an os.walk error is (rule 7)."""
    for item in items:
        top = os.path.join(base_dir, item)
        if os.path.islink(top):
            warnings.append(f"{root}/{item} is a symlink, skipped")
            continue
        if not os.path.exists(top):
            continue
        if os.path.isfile(top):
            if item not in SKIP_NAMES:
                yield item
            continue
        if not os.access(top, os.R_OK | os.X_OK):
            warnings.append(f"{root}/{item} is not readable, skipped")
            continue

        def onerror(exc, _item=item):
            warnings.append(f"error walking {root}/{_item}: {exc}")

        for dirpath, dirnames, filenames in os.walk(top, onerror=onerror):
            dirnames[:] = [d for d in dirnames
                           if d not in SKIP_NAMES and not os.path.islink(os.path.join(dirpath, d))
                           and os.path.relpath(os.path.join(dirpath, d), base_dir).replace(os.sep, "/")
                           not in SKIP_PATHS]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                if fn in SKIP_NAMES or os.path.islink(full):
                    continue
                yield os.path.relpath(full, base_dir).replace(os.sep, "/")


def _folder_cwd(folder_dir, folder, given_folders, m):
    """The folder's absolute and neutral cwd, from its first transcript or the folders arg."""
    try:
        names = sorted(f for f in os.listdir(folder_dir)
                        if f.endswith(".jsonl") and not os.path.islink(os.path.join(folder_dir, f)))
    except OSError:
        names = []
    for name in names:
        with open(os.path.join(folder_dir, name), "rb") as fh:
            head = fh.read(1 << 20)
        cwd = transcript_cwd(head[:head.rfind(b"\n") + 1])
        if cwd:
            return cwd, neutral(cwd, m)
    neutral_cwd = given_folders.get(folder)
    if neutral_cwd:
        return localize(neutral_cwd, m), neutral_cwd
    return None, None


def cmd_inventory(args, stdin):
    m = Machine("here", args["home"], args["desktop"])
    cache = json.loads(stdin.decode()) if stdin else {}
    given_folders = args.get("folders", {})
    prefixes = args.get("prefixes", ())  # rule 1: [home, desktop] pairs of all machines
    files, warnings, out_folders, folder_cwd_cache = {}, [], {}, {}

    def hash_of(key, data):
        return hashlib.sha256(normalize_bytes(key, data, m, prefixes=prefixes)).hexdigest()

    def handle(root, base_dir, items):
        for rel in _walk_root(base_dir, items, root, warnings):
            folder_cwd = None
            if root == "cli" and rel.startswith("projects/"):
                parts = rel.split("/", 2)
                folder = parts[1] if len(parts) >= 2 else None
                if folder not in folder_cwd_cache:
                    folder_dir = os.path.join(base_dir, "projects", folder)
                    folder_cwd_cache[folder] = _folder_cwd(folder_dir, folder, given_folders, m)
                folder_cwd, neutral_cwd = folder_cwd_cache[folder]
                if neutral_cwd:
                    out_folders[folder] = neutral_cwd
            key = key_for(root, rel, m, folder_cwd)
            if key is None:
                warnings.append(f"no key for {root}/{rel}")
                continue
            full = os.path.join(base_dir, rel)
            st = os.stat(full)
            mtime = int(st.st_mtime)
            cached = cache.get(key)
            # ponytail: a transcript with a partial last line is read each run; its cached size is shorter.
            if cached and cached[0] == mtime and cached[1] == st.st_size:
                files[key] = [cached[2], mtime, st.st_size]
                continue
            with open(full, "rb") as fh:
                data = fh.read()
            if classify(key) == "transcript":
                cut = data.rfind(b"\n")
                data = data[:cut + 1] if cut >= 0 else b""
            size = len(data)
            if cached and cached[0] == mtime and cached[1] == size:
                digest = cached[2]
            else:
                digest = hash_of(key, data)
            files[key] = [digest, mtime, size]

    handle("cli", f"{m.home}/.claude", CLI_ITEMS)
    handle("desktop", m.desktop, DESKTOP_ITEMS)
    return {"files": files, "folders": out_folders, "warnings": warnings}


# --- pack / apply / delete / undo -------------------------------------------------------

def cmd_pack(args, stdin):
    m = Machine("here", args["home"], args["desktop"])
    keys = json.loads(stdin)["keys"]  # rule 4: keys travel on stdin, not the ssh argument
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w|gz") as tar:
        for key in keys:
            if not safe_key(key):
                raise ValueError(f"unsafe key: {key}")
            path = key_to_path(key, m)
            try:
                with open(path, "rb") as f:
                    data = f.read()
                st = os.stat(path)
            except FileNotFoundError:
                continue  # deleted since the file list; the cli sees it missing and skips the key
            if classify(key) == "transcript":
                cut = data.rfind(b"\n")
                data = data[:cut + 1] if cut >= 0 else b""
            info = tarfile.TarInfo(key)
            info.size = len(data)
            info.mtime = int(st.st_mtime)
            info.mode = _exec_mode(st.st_mode)  # rule 6
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _load_manifest(backup_dir):
    p = f"{backup_dir}/manifest.json"
    return json.loads(open(p, "rb").read()) if os.path.isfile(p) else {}


def _backup(target, key, backup_dir, root, manifest):
    """Record the file as it was before this run. A run can write one key more than once,
    so only the first call keeps a backup: undo must restore the state before the run."""
    if key in manifest:
        return
    backup_path = None
    if os.path.exists(target):
        backup_path = f"{backup_dir}/{key}"
        _mkdirs_secure(os.path.dirname(backup_path), root)
        shutil.copy2(target, backup_path)
    manifest[key] = {"path": target, "backup": backup_path, "written": None}


def cmd_apply(args, stdin):
    dst = Machine("here", args["home"], args["desktop"])
    src = Machine("there", args["src"]["home"], args["src"]["desktop"])
    run_id = args["run_id"]
    root = _cache_root(dst)
    backup_dir = f"{root}/backup/{run_id}"
    _mkdirs_secure(backup_dir, root)
    written, errors, skipped = {}, [], []
    manifest = _load_manifest(backup_dir)
    expect = {}
    with tarfile.open(fileobj=io.BytesIO(stdin), mode="r:gz") as tar:
        for i, member in enumerate(tar):
            if not member.isfile():
                continue
            try:
                data = tar.extractfile(member).read()
                if i == 0 and member.name == EXPECT_MEMBER:  # rule 5
                    expect = json.loads(data) if data else {}
                    continue
                mode = _exec_mode(member.mode)  # rule 6
                if member.name.startswith("conflicts/"):
                    rel = member.name[len("conflicts/"):]
                    if not safe_key(rel):  # rule 3
                        errors.append(f"{member.name}: unsafe key")
                        continue
                    target = f"{root}/conflicts/{run_id}/{rel}"
                    if not _under_root(target, f"{root}/conflicts/{run_id}"):
                        errors.append(f"{member.name}: target escapes its root")
                        continue
                    _mkdirs_secure(os.path.dirname(target), root)
                    _atomic_write(target, data, mtime=member.mtime, mode=mode)
                    continue
                key = member.name
                if not safe_key(key):  # rule 3
                    errors.append(f"{key}: unsafe key")
                    continue
                target = key_to_path(key, dst)
                if not _under_root(target, _key_root_dir(key, dst)):
                    errors.append(f"{key}: target escapes its root")
                    continue
                if key in expect and _expect_mismatch(target, expect[key]):  # rule 5
                    skipped.append(key)
                    continue
                localized = localize_bytes(key, data, src, dst)
                _backup(target, key, backup_dir, root, manifest)
                _atomic_write(target, localized, mtime=member.mtime, mode=mode)
                manifest[key]["written"] = [int(member.mtime), len(localized)]
                written[key] = [int(member.mtime), len(localized)]
            except Exception as e:
                errors.append(f"{member.name}: {e}")
    _atomic_write(f"{backup_dir}/manifest.json", json.dumps(manifest).encode())
    return {"written": written, "errors": errors, "skipped": skipped}


def cmd_delete(args, stdin):
    m = Machine("here", args["home"], args["desktop"])
    run_id = args["run_id"]
    payload = json.loads(stdin)  # rule 4: keys (and expect) travel on stdin
    keys, expect = payload["keys"], payload.get("expect", {})
    root = _cache_root(m)
    backup_dir = f"{root}/backup/{run_id}"
    _mkdirs_secure(backup_dir, root)
    deleted, errors, skipped = [], [], []
    manifest = _load_manifest(backup_dir)
    for key in keys:
        try:
            if not safe_key(key):  # rule 3
                errors.append(f"{key}: unsafe key")
                continue
            target = key_to_path(key, m)
            if key in expect and _expect_mismatch(target, expect[key]):  # rule 5
                skipped.append(key)
                continue
            _backup(target, key, backup_dir, root, manifest)
            if os.path.exists(target):
                os.remove(target)
                deleted.append(key)
        except Exception as e:
            errors.append(f"{key}: {e}")
    _atomic_write(f"{backup_dir}/manifest.json", json.dumps(manifest).encode())
    return {"deleted": deleted, "errors": errors, "skipped": skipped}


def cmd_undo(args, stdin):
    m = Machine("here", args["home"], args["desktop"])
    run_id = args["run_id"]  # rule 9: no more "newest run" fallback
    root = _cache_root(m)
    backup_dir = f"{root}/backup/{run_id}"
    mf_path = f"{backup_dir}/manifest.json"
    if not os.path.isfile(mf_path):
        return {"restored": [], "removed": [], "skipped": []}
    manifest = json.loads(open(mf_path, "rb").read())
    undo_dir = f"{root}/backup/undo-{run_id}"
    restored, removed, skipped = [], [], []
    for key, info in manifest.items():
        target, backup, wrote = info["path"], info["backup"], info.get("written")
        exists = os.path.exists(target)
        current = [int(os.stat(target).st_mtime), os.stat(target).st_size] if exists else None
        # rule 9: skip a file that changed since the run instead of clobbering the change.
        if (wrote is None and exists) or (wrote is not None and current != list(wrote)):
            skipped.append(key)
            continue
        if exists:
            # rule 9: never remove or overwrite without a safety copy of the current file first.
            safety = f"{undo_dir}/{key}"
            _mkdirs_secure(os.path.dirname(safety), root)
            shutil.copy2(target, safety)
        if backup:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(backup, target)
            restored.append(target)
        elif exists:
            os.remove(target)
            removed.append(target)
    return {"restored": restored, "removed": removed, "skipped": skipped, "keys": sorted(manifest)}


def cmd_prune(args, stdin):
    m = Machine("here", args["home"], args["desktop"])
    backup_root = f"{_cache_root(m)}/backup"
    cutoff = time.time() - args["days"] * 86400
    removed = []
    if os.path.isdir(backup_root):
        for rid in os.listdir(backup_root):
            p = f"{backup_root}/{rid}"
            if os.path.isdir(p) and os.path.getmtime(p) < cutoff:
                shutil.rmtree(p)
                removed.append(rid)
    return {"removed": removed}


# --- lock / state / proc_alive -----------------------------------------------------------

def cmd_lock_read(args, stdin):
    p = f"{_cache_root(Machine('here', args['home'], args['desktop']))}/{LOCK_NAME}"
    return {"lock": json.loads(open(p, "rb").read()) if os.path.isfile(p) else None}


def cmd_lock_write(args, stdin):
    m = Machine("here", args["home"], args["desktop"])
    root = _cache_root(m)
    _mkdirs_secure(root, root)
    path = f"{root}/{LOCK_NAME}"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)  # rule 8: atomic create
    except FileExistsError:
        existing = json.loads(open(path, "rb").read()) if os.path.isfile(path) else None
        return {"ok": False, "lock": existing}
    with os.fdopen(fd, "wb") as f:
        f.write(json.dumps(args["lock"]).encode())
    os.chmod(path, 0o600)
    return {"ok": True, "lock": args["lock"]}


def cmd_lock_remove(args, stdin):
    p = f"{_cache_root(Machine('here', args['home'], args['desktop']))}/{LOCK_NAME}"
    if os.path.isfile(p):
        os.remove(p)
    return {"lock": None}


def cmd_state_read(args, stdin):
    p = f"{_cache_root(Machine('here', args['home'], args['desktop']))}/{STATE_NAME}"
    return {"state": json.loads(open(p, "rb").read()) if os.path.isfile(p) else None}


def cmd_state_write(args, stdin):
    root = _cache_root(Machine("here", args["home"], args["desktop"]))
    _mkdirs_secure(root, root)
    _atomic_write(f"{root}/{STATE_NAME}", stdin)
    return {"state": json.loads(stdin)}


def cmd_proc_alive(args, stdin):
    pid = args["pid"]
    try:
        os.kill(pid, 0)
    except OSError:
        return {"alive": False}
    res = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True)
    return {"alive": res.returncode == 0 and res.stdout.strip() == args["start"]}


COMMANDS = {
    "info": cmd_info,
    "inventory": cmd_inventory,
    "pack": cmd_pack,
    "apply": cmd_apply,
    "delete": cmd_delete,
    "lock_read": cmd_lock_read,
    "lock_write": cmd_lock_write,
    "lock_remove": cmd_lock_remove,
    "proc_alive": cmd_proc_alive,
    "state_read": cmd_state_read,
    "state_write": cmd_state_write,
    "undo": cmd_undo,
    "prune": cmd_prune,
}


def main(argv: list) -> int:
    """argv = [command, json args]. Writes the result to stdout."""
    command = argv[0]
    args = json.loads(argv[1]) if len(argv) > 1 and argv[1] else {}
    stdin = sys.stdin.buffer.read()
    try:
        result = COMMANDS[command](args, stdin)
    except Exception as e:
        sys.stderr.write(f"{type(e).__name__}: {e}\n")
        return 1
    if command == "pack":
        sys.stdout.buffer.write(result)
    else:
        sys.stdout.buffer.write(json.dumps(result).encode())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
