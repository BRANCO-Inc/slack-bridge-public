from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent


def load_script(module_name: str):
    path = PROJECT_ROOT / "scripts" / f"{module_name.rsplit('.', maxsplit=1)[-1]}.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


DOCTOR = load_script("scripts.doctor")
RUN = load_script("scripts.run")
SETUP = load_script("scripts.setup")


class OnboardingTests(unittest.TestCase):
    def test_setup_creates_private_env_and_preserves_existing_user_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(PROJECT_ROOT / "templates", root / "templates")
            with (
                patch.object(SETUP, "PROJECT_ROOT", root),
                patch.object(SETUP, "TEMPLATES_DIR", root / "templates"),
            ):
                SETUP.setup()

                env_path = root / ".env"
                self.assertEqual(env_path.stat().st_mode & 0o777, 0o600)
                manifest = (root / "config" / "slack-app-manifest.yaml").read_text(
                    encoding="utf-8"
                )
                for expected in (
                    "messages_tab_enabled: true",
                    "messages_tab_read_only_enabled: false",
                    "- app_mention",
                    "- message.im",
                    "- reaction_added",
                    "is_enabled: true",
                    "- app_mentions:read",
                    "- chat:write",
                    "- reactions:write",
                ):
                    self.assertIn(expected, manifest)

                identity_path = root / "IDENTITY.md"
                members_path = root / "config" / "members.json"
                identity_path.write_text("custom identity\n", encoding="utf-8")
                members_path.write_text(
                    '{"members": [{"slack_user_id": "U1", "cc_call": "owner"}]}\n',
                    encoding="utf-8",
                )
                env_path.write_text("SLACK_BRIDGE_AUTH_TOKEN=retained\n", encoding="utf-8")

                self.assertEqual(SETUP.setup(), [])
                self.assertEqual(identity_path.read_text(encoding="utf-8"), "custom identity\n")
                self.assertEqual(
                    members_path.read_text(encoding="utf-8"),
                    '{"members": [{"slack_user_id": "U1", "cc_call": "owner"}]}\n',
                )
                self.assertEqual(
                    env_path.read_text(encoding="utf-8"),
                    "SLACK_BRIDGE_AUTH_TOKEN=retained\n",
                )

                os.chmod(env_path, 0o644)
                SETUP.setup(confirm=SETUP.OVERWRITE_CONFIRM_EXACT)
                self.assertEqual(env_path.stat().st_mode & 0o777, 0o600)

    def test_doctor_loads_the_same_env_files_as_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text(
                "AI_WORKER_PROVIDER=claude\nSLACK_BRIDGE_HOOK_PORT=9123\n",
                encoding="utf-8",
            )
            (root / ".env.local").write_text(
                "AI_WORKER_PROVIDER=codex\nSLACK_BRIDGE_SHELL_BIN=/bin/sh\n",
                encoding="utf-8",
            )
            (root / ".env.example").write_text("EXAMPLE_ONLY=ignored\n", encoding="utf-8")

            with patch.object(RUN, "PROJECT_ROOT", root), patch.dict(os.environ, {}, clear=True):
                RUN.load_env_files()
                run_values = {
                    name: os.environ.get(name)
                    for name in (
                        "AI_WORKER_PROVIDER",
                        "SLACK_BRIDGE_HOOK_PORT",
                        "SLACK_BRIDGE_SHELL_BIN",
                        "EXAMPLE_ONLY",
                    )
                }

            with (
                patch.object(DOCTOR, "PROJECT_ROOT", root),
                patch.object(RUN, "PROJECT_ROOT", root),
                patch.dict(os.environ, {}, clear=True),
            ):
                DOCTOR.load_runtime_env()
                doctor_values = {
                    name: os.environ.get(name)
                    for name in (
                        "AI_WORKER_PROVIDER",
                        "SLACK_BRIDGE_HOOK_PORT",
                        "SLACK_BRIDGE_SHELL_BIN",
                        "EXAMPLE_ONLY",
                    )
                }

            self.assertEqual(doctor_values, run_values)
            self.assertEqual(run_values["AI_WORKER_PROVIDER"], "claude")
            self.assertEqual(run_values["SLACK_BRIDGE_HOOK_PORT"], "9123")
            self.assertEqual(run_values["SLACK_BRIDGE_SHELL_BIN"], "/bin/sh")
            self.assertIsNone(run_values["EXAMPLE_ONLY"])

    def test_doctor_reports_members_json_shapes_that_runtime_cannot_load(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_dir = root / "config"
            config_dir.mkdir()
            members_path = config_dir / "members.json"

            with patch.object(DOCTOR, "PROJECT_ROOT", root), patch.dict(os.environ, {}, clear=True):
                members_path.write_text("[\"not-an-object\"]\n", encoding="utf-8")
                check = DOCTOR.check_members()
                self.assertFalse(check.ok)
                self.assertIn("top-level JSON value must be an object", check.detail)

                members_path.write_text("{\"members\": [{\"slack_user_id\": \"U1\"}]}\n", encoding="utf-8")
                check = DOCTOR.check_members()
                self.assertFalse(check.ok)
                self.assertIn("members[0] is missing cc_call", check.detail)

                members_path.write_text(
                    '{"members": [{"slack_user_id": [], "cc_call": "owner"}]}\n',
                    encoding="utf-8",
                )
                check = DOCTOR.check_members()
                self.assertFalse(check.ok)
                self.assertIn("members[0].slack_user_id must be a string", check.detail)

                members_path.write_text(
                    '{"members": [{"slack_user_id": "U1", "cc_call": []}]}\n',
                    encoding="utf-8",
                )
                check = DOCTOR.check_members()
                self.assertFalse(check.ok)
                self.assertIn("members[0].cc_call must be a string", check.detail)

                members_path.write_text("{\"members\": []}\n", encoding="utf-8")
                self.assertTrue(DOCTOR.check_members().ok)

    def test_doctor_requires_the_project_venv_instead_of_a_python_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            venv_python = root / "venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.symlink_to(Path(sys.executable))

            with patch.object(DOCTOR, "PROJECT_ROOT", root):
                self.assertFalse(DOCTOR.check_running_from_venv().ok)

    def test_doctor_uses_runtime_shell_resolution(self):
        with patch.dict(
            os.environ,
            {"SLACK_BRIDGE_SHELL_BIN": "", "SHELL_BIN": "", "SHELL": "/bin/sh"},
            clear=True,
        ):
            check = DOCTOR.check_shell()
        self.assertTrue(check.ok)
        self.assertEqual(check.detail, "/bin/sh")

    def test_run_rejects_python_before_importing_runtime(self):
        stderr = io.StringIO()
        with patch.object(RUN.sys, "version_info", (3, 13, 9)), contextlib.redirect_stderr(stderr):
            self.assertEqual(RUN.main(), 2)
        self.assertIn("requires Python 3.14 or later", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
