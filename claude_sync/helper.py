"""Runs on each machine, local or over SSH as one program with paths.py. See docs/contract.md."""
import json
import sys

try:
    from claude_sync.model import Machine
    from claude_sync.paths import *  # noqa: F401,F403
except ImportError:  # inside the combined program the names already exist
    pass


def main(argv: list) -> int:
    """argv = [command, json args]. Writes the result to stdout."""
    raise NotImplementedError


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
