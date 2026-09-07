from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from scripts import doctor, run


class NativeWindowsRuntimeTest(unittest.TestCase):
    def test_doctor_explains_native_windows_is_unsupported(self) -> None:
        with patch.object(doctor.sys, "platform", "win32"):
            check = doctor.check_runtime_platform()

        self.assertFalse(check.ok)
        self.assertIn("Native Windows Python is unsupported", check.detail)
        self.assertIn("scripts\\windows.ps1", check.detail)

    def test_run_explains_native_windows_is_unsupported(self) -> None:
        stderr = io.StringIO()
        with patch.object(run.sys, "platform", "win32"), redirect_stderr(stderr):
            result = run.main()

        self.assertEqual(result, 2)
        self.assertIn("Native Windows Python is unsupported", stderr.getvalue())
        self.assertIn("scripts\\windows.ps1", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
