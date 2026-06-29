"""Tests for common.log."""

import logging
import sys
import unittest

from common import log


class SetupLoggingTests(unittest.TestCase):
    def setUp(self):
        self._orig_excepthook = sys.excepthook
        self._orig_handlers = list(logging.root.handlers)
        self._orig_level = logging.root.level

    def tearDown(self):
        sys.excepthook = self._orig_excepthook
        logging.root.handlers = self._orig_handlers
        logging.root.setLevel(self._orig_level)

    def test_installs_info_level_root_handler(self):
        logging.root.handlers = []
        log.setup_logging()
        self.assertEqual(logging.root.level, logging.INFO)
        self.assertTrue(logging.root.handlers)

    def test_replaces_excepthook(self):
        log.setup_logging()
        self.assertIsNot(sys.excepthook, self._orig_excepthook)

    def test_excepthook_logs_critical(self):
        log.setup_logging()
        with self.assertLogs(level='CRITICAL') as captured:
            try:
                raise RuntimeError('boom')
            except RuntimeError:
                sys.excepthook(*sys.exc_info())
        self.assertTrue(
            any('Uncaught exception' in line for line in captured.output),
            captured.output,
        )


if __name__ == '__main__':
    unittest.main()
