"""Structured JSON logging setup."""

import logging
import sys

from pythonjsonlogger.json import JsonFormatter

_HANDLER_NAME = 'energy_digest_json'
# Libraries that log full request URLs (and other detail) at INFO/DEBUG.
_NOISY_LOGGERS = ('httpx', 'httpcore', 'trafilatura', 'googlenewsdecoder', 'urllib3')


def configure_logging(level: str = 'INFO') -> None:
    """Install a single JSON stdout handler on the root logger (idempotent)."""
    root = logging.getLogger()
    root.setLevel(level)
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    if any(h.get_name() == _HANDLER_NAME for h in root.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(
        JsonFormatter(
            '%(asctime)s %(levelname)s %(name)s %(message)s',
            rename_fields={'levelname': 'level', 'asctime': 'timestamp'},
        )
    )
    root.addHandler(handler)
