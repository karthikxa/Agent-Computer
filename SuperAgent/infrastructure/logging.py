"""Logging configuration helpers."""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


def configure_logging(log_dir: str | Path) -> logging.Logger:
    """Configure application logging with daily rotation."""

    path = Path(log_dir)
    path.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("superagent")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s agent=%(agent_id)s task=%(task_id)s %(message)s"
        )
        error_handler = TimedRotatingFileHandler(path / "errors.log", when="midnight", backupCount=7, encoding="utf-8")
        error_handler.setFormatter(formatter)
        error_handler.setLevel(logging.ERROR)
        activity_handler = TimedRotatingFileHandler(path / "activity.log", when="midnight", backupCount=7, encoding="utf-8")
        activity_handler.setFormatter(formatter)
        activity_handler.setLevel(logging.INFO)
        logger.addHandler(error_handler)
        logger.addHandler(activity_handler)
    return logger
