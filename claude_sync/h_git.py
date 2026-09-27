"""Helper command for the git part: reports repos and worktrees under ~/dev. Read only,
never changes a repo except `git fetch`. See docs/contract.md, the git row. Python 3.9."""
import os
import platform
import shlex
import subprocess

try:
    from claude_sync.helper import COMMANDS
except ImportError:  # inside the combined program COMMANDS already exists
    pass

FETCH_TIMEOUT = 30
GIT_TIMEOUT = 10


def _git_out(path, args):
    try:
        res = subprocess.run(["git", "-C", path] + args, capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except Exception:
        return None
    return res.stdout.strip() if res.returncode == 0 else None


def _fetch(path, system):
    """git fetch, 30s timeout; on Linux inside `bash -ic` so ~/.bash_env tokens load.
    Never changes the repo otherwise. A failure is reported, not raised."""
    cmd = ["git", "-C", path, "fetch"]
    try:
        if system == "Linux":
            res = subprocess.run(["bash", "-ic", " ".join(shlex.quote(c) for c in cmd)],
                                 capture_output=True, text=True, timeout=FETCH_TIMEOUT)
        else:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=FETCH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return "git fetch timed out"
    except Exception as e:
        return str(e)
    return None if res.returncode == 0 else (res.stderr.strip() or "git fetch failed")


def _status(path):
    res = subprocess.run(["git", "-C", path, "status", "--porcelain"],
                         capture_output=True, text=True, timeout=GIT_TIMEOUT)
    changed, untracked = [], []
    for line in res.stdout.splitlines():
        if not line:
            continue
        (untracked if line[:2] == "??" else changed).append(line[3:])
    return changed, untracked


def _scan_one(path, system):
    fetch_warning = _fetch(path, system)
    origin = _git_out(path, ["config", "--get", "remote.origin.url"])
    branch = _git_out(path, ["rev-parse", "--abbrev-ref", "HEAD"])
    upstream = _git_out(path, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    ahead = behind = 0
    if upstream:
        counts = _git_out(path, ["rev-list", "--left-right", "--count", f"{upstream}...HEAD"])
        if counts and len(counts.split()) == 2:
            behind, ahead = (int(x) for x in counts.split())
    changed, untracked = _status(path)
    return {"path": path, "origin": origin, "branch": None if branch == "HEAD" else branch,
            "upstream": upstream, "ahead": ahead, "behind": behind,
            "changed": changed, "untracked": untracked, "fetch_warning": fetch_warning}


def _find_repos(dev_dir):
    """Repos under ~/dev, two levels deep: dev/x or dev/games/capsa."""
    if not os.path.isdir(dev_dir):
        return []
    repos = []
    for name in sorted(os.listdir(dev_dir)):
        top = os.path.join(dev_dir, name)
        if not os.path.isdir(top) or os.path.islink(top):
            continue
        if os.path.isdir(os.path.join(top, ".git")):
            repos.append(top)
            continue
        try:
            children = sorted(os.listdir(top))
        except OSError:
            continue
        for cname in children:
            sub = os.path.join(top, cname)
            if os.path.isdir(sub) and not os.path.islink(sub) and os.path.isdir(os.path.join(sub, ".git")):
                repos.append(sub)
    return repos


def _find_worktrees(repo_path):
    """Every worktree `git worktree list` knows about, besides the main one. git resolves
    symlinks in the paths it reports; map a worktree back under repo_path's own (possibly
    symlinked) form, so it still sits under the machine's home for paths.neutral()."""
    try:
        res = subprocess.run(["git", "-C", repo_path, "worktree", "list", "--porcelain"],
                             capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except Exception:
        return []
    if res.returncode != 0:
        return []
    listed = [line[len("worktree "):] for line in res.stdout.splitlines() if line.startswith("worktree ")]
    real_repo = os.path.realpath(repo_path)
    out = []
    for p in listed:
        if os.path.realpath(p) == real_repo:
            continue  # the main worktree itself
        if p.startswith(real_repo + os.sep):
            p = repo_path + p[len(real_repo):]
        out.append(p)
    return out


def cmd_git_scan(args, stdin):
    system = platform.system()
    dev_dir = f"{args['home']}/dev"
    repos = []
    for repo_path in _find_repos(dev_dir):
        repos.append(_scan_one(repo_path, system))
        for wt_path in _find_worktrees(repo_path):
            repos.append(_scan_one(wt_path, system))
    return {"repos": repos}


COMMANDS.update({"git_scan": cmd_git_scan})
