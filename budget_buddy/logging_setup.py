"""logging_setup.py — leveled, auditable logging for unattended runs. See
docs/dev/budget-buddy-plan.md, "Logging" for what belongs at each level.

Adds a custom TRACE level (5, below DEBUG's 10) for the noisiest detail
(HTTP retry internals, per-row parse detail) that would otherwise drown out
DEBUG's already-useful query/payload dumps.
"""
from __future__ import annotations

import logging
import sys

TRACE = 5
logging.addLevelName(TRACE, "TRACE")

_LEVELS = {"TRACE": TRACE, "DEBUG": logging.DEBUG, "INFO": logging.INFO,
           "WARNING": logging.WARNING, "ERROR": logging.ERROR}


def _trace(self: logging.Logger, message, *args, **kwargs) -> None:
    if self.isEnabledFor(TRACE):
        self._log(TRACE, message, args, **kwargs)


logging.Logger.trace = _trace  # type: ignore[attr-defined]


def configure_logging(level_name: str = "INFO") -> None:
    level = _LEVELS.get(level_name.upper())
    if level is None:
        raise ValueError(f"unknown log level {level_name!r} — one of {', '.join(_LEVELS)}")
    root = logging.getLogger("budget_buddy")
    root.setLevel(level)
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z"
    ))
    root.addHandler(handler)
    root.propagate = False
