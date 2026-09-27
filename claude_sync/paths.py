"""Machine-neutral paths, keys, path fields and hashes. See docs/contract.md."""
try:
    from claude_sync.model import Machine, HOME_TOKEN, DESKTOP_TOKEN
except ImportError:  # inside the combined helper program
    pass


def folder_name(cwd: str) -> str:
    """Claude Code's project folder name for cwd, with the 200-character cut and hash."""
    raise NotImplementedError


def neutral(path: str, m: "Machine") -> str:
    """Absolute path on m to neutral form. Paths outside home stay the same."""
    raise NotImplementedError


def localize(npath: str, m: "Machine") -> str:
    """Neutral path to an absolute path on m."""
    raise NotImplementedError


def key_for(root: str, rel: str, m: "Machine", folder_cwd: str | None) -> str | None:
    """Key for the file at <root dir of m>/<rel>. folder_cwd is the absolute cwd of the
    project folder when rel is under projects/. None when no key is possible."""
    raise NotImplementedError


def key_to_path(key: str, m: "Machine") -> str:
    """Absolute path on m for key."""
    raise NotImplementedError


def classify(key: str) -> str:
    """transcript | session | json | raw"""
    raise NotImplementedError


def is_exec(key: str) -> bool:
    raise NotImplementedError


def transcript_cwd(data: bytes) -> str | None:
    """cwd of the first transcript line that has one."""
    raise NotImplementedError


def normalize_bytes(key: str, data: bytes, m: "Machine") -> bytes:
    """Neutral form of the content, the input of FileInfo.hash."""
    raise NotImplementedError


def localize_bytes(key: str, data: bytes, src: "Machine", dst: "Machine") -> bytes:
    """Content from src written for dst: only path fields change."""
    raise NotImplementedError
