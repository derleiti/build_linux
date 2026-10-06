from __future__ import annotations
import json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from ailinux_kernel_builder import bug_reporter

class BugReporterTests(unittest.TestCase):
    def test_queue_redacts(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)
            with patch.object(bug_reporter,"_state_root",return_value=root), patch.object(bug_reporter,"_post",return_value=False):
                bug_reporter._config={"app":"Kernel Builder","repo":"build_linux","version":"test","channel":"test"}
                self.assertTrue(bug_reporter.submit_manual("password=never-store")["queued"])
            self.assertNotIn("never-store",json.dumps(json.loads((root/"pending-reports.json").read_text())))
    def test_selftest(self):
        with tempfile.TemporaryDirectory() as raw:
            with patch.object(bug_reporter,"_state_root",return_value=Path(raw)):
                self.assertTrue(bug_reporter.startup_selftest()["ok"])
if __name__=='__main__': unittest.main()
