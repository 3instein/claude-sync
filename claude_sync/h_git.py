"""Helper command for the git part: reports repos and worktrees under ~/dev. Read only,
never changes a repo except `git fetch`. See docs/contract.md, the git row. Python 3.9."""
import os
import platform
import shlex
import signal
import subprocess

try:
    from claude_sync.helper import COMMANDS
except ImportError:  # inside the combined program COMMANDS already exists
    pass

FETCH_TIMEOUT = 30
GIT_TIMEOUT = 10


def _run_git(argv, timeout, use_bash_ic=False):
    """Run a git command, or (for fetch, on Linux) the same command inside `bash -ic` so
    ~/.bash_env tokens load. Runs in its own process group so a timeout kills the whole
    tree (git can spawn ssh, an askpass prompt, a credential helper, ...), not only the
    immediate child. Returns (returncode, stdout, stderr); returncode is None on a timeout
    or a launch failure, and stderr then holds "timed out" or the launch exception."""
    cmd = ["bash", "-ic", " ".join(shlex.quote(c) for c in argv)] if use_bash_ic else argv
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
    except Exception as e:
        return None, "", str(e)
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            pass
        try:  # a child outside the group (a credential daemon) can keep the pipes open
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            for pipe in (proc.stdout, proc.stderr):
                if pipe:
                    pipe.close()
            proc.wait(timeout=5)
        return None, "", "timed out"


def _fail(what, err):
    return f"{what} timed out" if err == "timed out" else f"{what} failed: {(err or '').strip() or 'unknown error'}"


def _git_out(path, args):
    code, out, _ = _run_git(["git", "-C", path] + args, GIT_TIMEOUT)
    return out.strip() if code == 0 else None


def _fetch(path, system):
    """git fetch, 30s timeout; on Linux inside `bash -ic` so ~/.bash_env tokens load.
    Never changes the repo otherwise. A failure or a timeout is reported, not raised."""
    code, _, err = _run_git(["git", "-C", path, "fetch"], FETCH_TIMEOUT, use_bash_ic=(system == "Linux"))
    return None if code == 0 else _fail("git fetch", err)


def _status(path):
    code, out, err = _run_git(["git", "-C", path, "status", "--porcelain"], GIT_TIMEOUT)
    if code != 0:
        return [], [], _fail("git status", err)
    changed, untracked = [], []
    for line in out.splitlines():
        if not line:
            continue
        (untracked if line[:2] == "??" else changed).append(line[3:])
    return changed, untracked, None


def _scan_one(path, system, kind):
    fetch_warning = _fetch(path, system)
    origin = _git_out(path, ["config", "--get", "remote.origin.url"])
    branch = _git_out(path, ["rev-parse", "--abbrev-ref", "HEAD"])
    upstream = _git_out(path, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    ahead = behind = 0
    if upstream:
        counts = _git_out(path, ["rev-list", "--left-right", "--count", f"{upstream}...HEAD"])
        if counts and len(counts.split()) == 2:
            behind, ahead = (int(x) for x in counts.split())
    changed, untracked, status_warning = _status(path)
    return {"path": path, "kind": kind, "origin": origin, "branch": None if branch == "HEAD" else branch,
            "upstream": upstream, "ahead": ahead, "behind": behind, "changed": changed,
            "untracked": untracked, "fetch_warning": fetch_warning, "status_warning": status_warning}


def _find_repos(dev_dir):
    """Repos under ~/dev, two levels deep: dev/x or dev/games/capsa. A folder whose .git
    is a file (a worktree) is never itself walked as a top-level repo here."""
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
    code, out, _ = _run_git(["git", "-C", repo_path, "worktree", "list", "--porcelain"], GIT_TIMEOUT)
    if code != 0:
        return []
    listed = [line[len("worktree "):] for line in out.splitlines() if line.startswith("worktree ")]
    real_repo = os.path.realpath(repo_path)
    out_paths = []
    for p in listed:
        if os.path.realpath(p) == real_repo:
            continue  # the main worktree itself
        if p.startswith(real_repo + os.sep):
            p = repo_path + p[len(real_repo):]
        out_paths.append(p)
    return out_paths


def cmd_git_scan(args, stdin):
    system = platform.system()
    dev_dir = f"{args['home']}/dev"
    repos = []
    for repo_path in _find_repos(dev_dir):
        repos.append(_scan_one(repo_path, system, "repo"))
        for wt_path in _find_worktrees(repo_path):
            repos.append(_scan_one(wt_path, system, "worktree"))
    return {"repos": repos}


COMMANDS.update({"git_scan": cmd_git_scan})
