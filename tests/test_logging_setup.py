import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from copytree import logging_setup  # noqa: E402


class StderrNoneTest(unittest.TestCase):
    """sys.stderr 为 None（GUI 子系统无控制台）时日志初始化不得崩溃。"""

    def setUp(self):
        logging_setup._configured = False  # 复位幂等标记

    def test_enable_stderr_with_none_stderr_does_not_raise(self):
        with mock.patch.object(logging_setup, "LOG_DIR"), \
             mock.patch("os.makedirs"), \
             mock.patch.object(sys, "stderr", None), \
             mock.patch.object(logging_setup.logger, "add") as add:
            logging_setup.setup_logging(enable_stderr=True)  # 不应抛 TypeError
        sinks = [c.args[0] for c in add.call_args_list]
        self.assertNotIn(None, sinks)

    def test_fallback_branch_with_none_stderr_does_not_raise(self):
        with mock.patch.object(logging_setup, "LOG_DIR"), \
             mock.patch("os.makedirs", side_effect=OSError("readonly")), \
             mock.patch.object(sys, "stderr", None), \
             mock.patch.object(logging_setup.logger, "add") as add:
            logging_setup.setup_logging(enable_stderr=False)
        sinks = [c.args[0] for c in add.call_args_list]
        self.assertNotIn(None, sinks)

    def test_stderr_disabled_with_none_stderr_adds_no_extra_sink(self):
        # 空输入/极值边界：enable_stderr=False 时即便 stderr 为 None 也不添加任何 sink
        with mock.patch.object(logging_setup, "LOG_DIR"), \
             mock.patch("os.makedirs"), \
             mock.patch.object(sys, "stderr", None), \
             mock.patch.object(logging_setup.logger, "add") as add:
            logging_setup.setup_logging(enable_stderr=False)
        sinks = [c.args[0] for c in add.call_args_list]
        self.assertNotIn(None, sinks)
        self.assertEqual(len(sinks), 1)  # 仅文件 sink


if __name__ == "__main__":
    unittest.main()
