"""Logging setup shared by all reports."""

import logging
import sys

LOG_FORMAT = '%(asctime)s %(levelname)s: %(message)s'


def setup_logging():
    """Configure root logging and route uncaught exceptions through it."""
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    logger = logging.getLogger(__name__)

    def _log_uncaught(exc_type, exc_value, exc_tb):
        logger.critical(
            "Uncaught exception",
            exc_info=(exc_type, exc_value, exc_tb),
        )

    sys.excepthook = _log_uncaught
