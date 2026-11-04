"""Logging setup with secret redaction as a last line of defence.

Tokens are never put into URLs, git config or command lines in the first
place; this filter only guards against them leaking through error bodies.
"""

from __future__ import annotations

import logging
import sys
import time

log = logging.getLogger("mirror")

_secrets: list[str] = []


def register_secret(value: str | None) -> None:
    if value and len(value) >= 4 and value not in _secrets:
        _secrets.append(value)
        _secrets.sort(key=len, reverse=True)


def clear_secrets() -> None:
    _secrets.clear()


def redact(text: str) -> str:
    for s in _secrets:
        if s in text:
            text = text.replace(s, "***")
    return text


class RedactingFormatter(logging.Formatter):
    converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RedactingFormatter("%(asctime)sZ %(levelname)-7s %(message)s", "%Y-%m-%dT%H:%M:%S"))
    log.handlers[:] = [handler]
    log.setLevel(level)
    log.propagate = False
