"""Runs helper commands locally or on the other machine over SSH. See docs/contract.md."""


class RemoteError(Exception):
    pass


class Runner:
    def __init__(self, host: str | None):
        self.host = host

    def call(self, command: str, args: dict, stdin: bytes = b"") -> bytes:
        raise NotImplementedError


def program() -> str:
    """The combined base64 program: paths.py + helper.py source."""
    raise NotImplementedError
