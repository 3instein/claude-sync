"""Runs helper commands locally or on the other machine over SSH. See docs/contract.md."""
import base64
import zlib
import json
import shlex
import subprocess
import sys
from pathlib import Path

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ForwardAgent=no"]


class RemoteError(Exception):
    pass


class Runner:
    def __init__(self, host: str | None):
        self.host = host

    def call(self, command: str, args: dict, stdin: bytes = b"") -> bytes:
        code = bootstrap()
        if self.host is None:
            argv = [sys.executable, "-c", code, command, json.dumps(args)]
        else:
            remote_cmd = " ".join(shlex.quote(p) for p in ["python3", "-c", code, command, json.dumps(args)])
            argv = ["ssh", *SSH_OPTS, self.host, remote_cmd]
        proc = subprocess.run(argv, input=stdin, capture_output=True)
        if proc.returncode != 0:
            raise RemoteError(proc.stderr.decode(errors="replace"))
        return proc.stdout


def program() -> str:
    """The combined base64 program: model.py + paths.py + helper.py source."""
    here = Path(__file__).parent
    # A non-interactive SSH login on the Mac gets /usr/bin/python3 (3.9), so the program
    # must parse there: postponed annotations make `str | None` legal on 3.9.
    # The program must not import the claude_sync package from the working folder, which
    # may hold another version: drop the -c entry ("") from sys.path first.
    # Part helpers (h_*.py) come after helper.py and add their commands to COMMANDS.
    names = ["model.py", "paths.py", "helper.py"] + sorted(p.name for p in here.glob("h_*.py"))
    src = ("from __future__ import annotations\nimport sys\nsys.path[:] = [p for p in sys.path if p]\n"
           + "\n".join((here / name).read_text() for name in names)
           + "\nsys.exit(main(sys.argv[1:]))\n")
    # zlib keeps the program far below the 128 KB limit of one SSH argument as parts are added.
    return base64.b64encode(zlib.compress(src.encode(), 9)).decode()


def bootstrap() -> str:
    """The python3 -c code that unpacks and runs program()."""
    return f"import base64,zlib;exec(zlib.decompress(base64.b64decode('{program()}')))"
