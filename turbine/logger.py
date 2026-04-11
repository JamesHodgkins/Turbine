"""Turbine logger — tracks Thinking and Action phases."""

import logging
import sys
from enum import Enum


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


class TurbineLogger:
    def __init__(self, name: str = "turbine"):
        self._log = logging.getLogger(name)
        if not self._log.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(
                logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
            )
            self._log.addHandler(handler)
            self._log.setLevel(logging.DEBUG)

    def thinking(self, message: str) -> None:
        self._log.info(f"[THINKING] {message}")

    def action(self, message: str) -> None:
        self._log.info(f"[ACTION]   {message}")

    def error(self, message: str) -> None:
        self._log.error(f"[ERROR]    {message}")

    def debug(self, message: str) -> None:
        self._log.debug(f"[DEBUG]    {message}")
