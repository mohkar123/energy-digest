import logging

from energy_digest.logging_config import configure_logging


def test_configure_logging_is_idempotent() -> None:
    root = logging.getLogger()
    before = len(root.handlers)
    configure_logging()
    configure_logging()
    assert len(root.handlers) == before + 1


def test_chatty_http_libraries_are_quieted() -> None:
    configure_logging('DEBUG')
    for name in ('httpx', 'httpcore', 'trafilatura', 'googlenewsdecoder', 'urllib3'):
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING
