"""Turbine logger — tracks Thinking and Action phases."""

import logging
import sys
from datetime import datetime
from enum import Enum

from rich.console import Console
from rich.text import Text


class Phase(Enum):
    THINKING = "THINKING"
    ACTION = "ACTION"


def get_logger(name: str = "turbine") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("[%(asctime)s] [%(phase)s] %(message)s", datefmt="%H:%M:%S")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
    return logger


# Shared console — force_terminal so colours work even when piped during tests
_console = Console(stderr=False, highlight=False, force_terminal=None)


class TurbineLogger:
    def __init__(self, name: str = "turbine"):
        # Keep the stdlib logger for any existing code that reads log records,
        # but silence its handler — we print via Rich instead.
        self._log = logging.getLogger(name)
        if not self._log.handlers:
            self._log.addHandler(logging.NullHandler())
        self._log.setLevel(logging.DEBUG)

    # ------------------------------------------------------------------
    # Internal helper
    # ------------------------------------------------------------------

    @staticmethod
    def _ts() -> str:
        return datetime.now().strftime("%H:%M:%S")

    def _print(self, label: str, label_style: str, message: str) -> None:
        line = Text()
        line.append(f"[{self._ts()}] ", style="dim")
        line.append(f"[{label}]", style=label_style)
        line.append(f"  {message}")
        _console.print(line)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def thinking(self, message: str) -> None:
        self._print("THINKING", "bold cyan", message)

    def action(self, message: str) -> None:
        self._print("ACTION  ", "bold green", message)

    def error(self, message: str) -> None:
        self._print("ERROR   ", "bold red", message)

    def debug(self, message: str) -> None:
        self._print("DEBUG   ", "dim", message)

    def verbose(self, message: str) -> None:
        """Extra detail — only call when verbose mode is active."""
        self._print("VERBOSE ", "dim magenta", message)
