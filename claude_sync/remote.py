"""Runs helper commands locally or on the other machine over SSH. See docs/contract.md."""
import base64
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
        code = f"import base64;exec(base64.b64decode('{program()}'))"
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
    src = "\n".join((here / name).read_text() for name in ("model.py", "paths.py", "helper.py"))
    return base64.b64encode(src.encode()).decode()
