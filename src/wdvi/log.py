"""Logging-Setup (eine Stelle für Format/Level)."""

from __future__ import annotations

import logging
import sys


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    for noisy in ("httpx", "httpcore", "urllib3", "googleapiclient.discovery_cache", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
