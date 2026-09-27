"""Three-way merge plan and content-level merges. Pure functions. See docs/contract.md."""
from dataclasses import dataclass

from claude_sync.model import Plan


@dataclass
class Opts:
    here_host: str
    there_host: str
    now: int
    first_run_cutoff: dict
    cleanup_days: int = 30
    deletion_limit: int = 50
    live_window: int = 60
    confirm: str | None = None
    keep: tuple = ()


def plan(state_files: dict, here: dict, there: dict, opts: Opts) -> Plan:
    raise NotImplementedError


def review_token(review: dict) -> str:
    raise NotImplementedError


def resolve_transcript(here: bytes, there: bytes) -> str:
    """here | there | split"""
    raise NotImplementedError


def split_transcript(data: bytes, old_id: str, new_id: str) -> bytes:
    raise NotImplementedError


def merge_json(base, here: dict, there: dict, here_is_newer: bool) -> tuple:
    """(merged, overridden key names)"""
    raise NotImplementedError
