"""Helper command for the secrets report part. See docs/contract.md, Phase 2, `secrets`.
Runs on both machines, so it must stay Python 3.9 compatible like helper.py."""
import hashlib
import os

try:
    from claude_sync.helper import COMMANDS
    from claude_sync.model import Machine, CLI_ITEMS, SKIP_PATHS, DEV_SKIP_DIRS
    from claude_sync.paths import is_secret, neutral
except ImportError:  # inside the combined helper program
    pass


def _dev_secret_files(home):
    """Secret files anywhere under <home>/dev, repos included, dependency folders and
    symlinks excluded. Unlike the normal dev walk, this does not skip git repos: a
    secret checked into a repo by mistake is exactly what this report is for."""
    top = os.path.join(home, "dev")
    if not os.path.exists(top) or os.path.islink(top):
        return
    for dirpath, dirnames, filenames in os.walk(top):
        dirnames[:] = [d for d in dirnames if d not in DEV_SKIP_DIRS and d != ".git"
                       and not os.path.islink(os.path.join(dirpath, d))]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            if not os.path.islink(full) and is_secret(fn):
                yield full


def _claude_secret_files(home):
    """Secret files under the synced folders of <home>/.claude (model.CLI_ITEMS)."""
    base = os.path.join(home, ".claude")
    for item in CLI_ITEMS:
        top = os.path.join(base, item)
        if os.path.islink(top) or not os.path.exists(top):
            continue
        if os.path.isfile(top):
            if is_secret(item):
                yield top
            continue
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = [d for d in dirnames
                           if not os.path.islink(os.path.join(dirpath, d))
                           and os.path.relpath(os.path.join(dirpath, d), base).replace(os.sep, "/")
                           not in SKIP_PATHS]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                if not os.path.islink(full) and is_secret(fn):
                    yield full


def cmd_secrets_scan(args, stdin):
    m = Machine("here", args["home"], args["desktop"])
    out = {}
    for full in list(_dev_secret_files(m.home)) + list(_claude_secret_files(m.home)):
        with open(full, "rb") as f:
            out[neutral(full, m)] = hashlib.sha256(f.read()).hexdigest()
    return {"secrets": out}


COMMANDS.update({"secrets_scan": cmd_secrets_scan})
