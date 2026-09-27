"""Shared types. Owned by the orchestrator: agents read this file and never change it."""
from dataclasses import dataclass, field

HOME_TOKEN = "~"             # neutral form of a machine's home folder
DESKTOP_TOKEN = "{desktop}"  # neutral form of a machine's desktop data folder
ROOTS = ("cli", "desktop")   # cli = ~/.claude, desktop = the desktop data folder

# Phase 1 items under each root. Everything else under a root is out of scope.
CLI_ITEMS = ("CLAUDE.md", "settings.json", ".i-have-adhd-always", ".ponytail-active",
             "skills", "agents", "commands", "plans", "projects", "file-history", "uploads", "plugins/data")
DESKTOP_ITEMS = ("claude-code-sessions", "scratch-workspaces")
SKIP_NAMES = ("scheduled-tasks.json", ".DS_Store")
# The desktop app syncs these itself from claude.ai, per machine.
SKIP_PATHS = ("skills/synced",)
# Dependency/build folders under ~/dev that the dev root never walks into.
DEV_SKIP_DIRS = ("node_modules", ".venv", "venv", "__pycache__", "dist", "build",
                  ".next", ".turbo", "target", ".worktrees", "google-cloud-sdk")


@dataclass(frozen=True)
class Machine:
    name: str      # "here", or the SSH host of the other machine
    home: str      # absolute, no trailing slash
    desktop: str   # absolute desktop data folder, no trailing slash
    host: str = ""  # hostname, the key for per-machine data in the state


@dataclass(frozen=True)
class FileInfo:
    hash: str   # sha256 hex of paths.normalize_bytes() of the content
    mtime: int  # whole seconds
    size: int   # bytes; for .jsonl only up to the last full line


# Inventory = dict[key, FileInfo]. A key is machine-neutral, see docs/contract.md.


@dataclass(frozen=True)
class Action:
    op: str       # copy | delete | json_merge | transcript | conflict
    key: str
    to: str       # side that is written: "here" or "there"


@dataclass
class Plan:
    actions: list = field(default_factory=list)   # list[Action]
    stops: list = field(default_factory=list)     # subset of: deletions, first_run, exec_config
    review: dict = field(default_factory=dict)    # stop reason -> sorted list of [side, key]
    token: str = ""                               # merge.review_token(review)
    skipped_live: list = field(default_factory=list)  # keys not written here (live file rule)
