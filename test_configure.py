from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def load_script(module_name: str):
    path = SCRIPTS_DIR / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


CONFIGURE = load_script("configure")
RUN = load_script("run")


class ConfigureTests(unittest.TestCase):
    def project(self):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        shutil.copytree(PROJECT_ROOT / "templates", root / "templates")
        return directory, root

    @contextlib.contextmanager
    def configured_root(self, root: Path):
        with (
            patch.object(CONFIGURE, "PROJECT_ROOT", root),
            patch.object(CONFIGURE.SETUP, "PROJECT_ROOT", root),
            patch.object(CONFIGURE.SETUP, "TEMPLATES_DIR", root / "templates"),
            patch.dict(os.environ, {}, clear=True),
        ):
            yield

    def runtime_values(self, root: Path, keys: tuple[str, ...]) -> dict[str, str | None]:
        with patch.object(RUN, "PROJECT_ROOT", root), patch.dict(os.environ, {}, clear=True):
            RUN.load_env_files()
            return {key: os.environ.get(key) for key in keys}

    def test_new_install_applies_brand_without_credentials(self):
        directory, root = self.project()
        with directory, self.configured_root(root):
            self.assertEqual(
                CONFIGURE.main(
                    [
                        "--company",
                        "Acme Co.",
                        "--bot-name",
                        "Acme Bridge",
                        "--provider",
                        "codex",
                        "--apply",
                    ]
                ),
                0,
            )

            env = (root / ".env").read_text(encoding="utf-8")
            self.assertIn('SLACK_BRIDGE_COMPANY="Acme Co."', env)
            self.assertIn('SLACK_BRIDGE_BOT_NAME="Acme Bridge"', env)
            self.assertIn('AI_WORKER_PROVIDER="codex"', env)
            self.assertIn("SLACK_BOT_TOKEN=<slack-bot-token>", env)
            self.assertIn("SLACK_APP_TOKEN=<slack-app-token>", env)

            manifest = (root / "config" / "slack-app-manifest.yaml").read_text(
                encoding="utf-8"
            )
            self.assertIn('name: "Acme Bridge"', manifest)
            self.assertIn('display_name: "Acme Bridge"', manifest)

            identity = (root / "IDENTITY.md").read_text(encoding="utf-8")
            self.assertIn("- Name: Acme Bridge", identity)
            self.assertIn("- Company: Acme Co.", identity)

    def test_unrelated_apply_keeps_persisted_bot_under_process_override(self):
        directory, root = self.project()
        with directory, self.configured_root(root), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                CONFIGURE.main(["--bot-name", "Persisted Bot", "--apply"]), 0
            )
            with patch.dict(os.environ, {"SLACK_BRIDGE_BOT_NAME": "Transient Bot"}):
                self.assertEqual(CONFIGURE.main(["--company", "Acme", "--apply"]), 0)
            values = self.runtime_values(root, ("SLACK_BRIDGE_BOT_NAME",))
            self.assertEqual(values["SLACK_BRIDGE_BOT_NAME"], "Persisted Bot")
            manifest = (root / "config/slack-app-manifest.yaml").read_text(encoding="utf-8")
            identity = (root / "IDENTITY.md").read_text(encoding="utf-8")
            self.assertEqual(manifest.count('"Persisted Bot"'), 2)
            self.assertIn("- Name: Persisted Bot", identity)
            self.assertIn("- Company: Acme", identity)
            self.assertNotIn("Transient Bot", manifest + identity)

    def test_second_run_preserves_credentials_and_unrelated_identity(self):
        directory, root = self.project()
        with directory, self.configured_root(root):
            self.assertEqual(
                CONFIGURE.main(
                    ["--company", "Acme", "--bot-name", "Bridge", "--apply"]
                ),
                0,
            )
            env_path = root / ".env"
            env_path.write_text(
                env_path.read_text(encoding="utf-8")
                + "SLACK_BOT_TOKEN=xoxb-retained-token\n"
                + "SLACK_APP_TOKEN=xapp-retained-token\n"
                + "CUSTOM_SETTING=retained\n",
                encoding="utf-8",
            )
            identity_path = root / "IDENTITY.md"
            identity_path.write_text(
                identity_path.read_text(encoding="utf-8")
                + "\n- Role: Custom role\n- Tone: Custom tone\n",
                encoding="utf-8",
            )

            self.assertEqual(CONFIGURE.main(["--bot-name", "Bridge Two", "--apply"]), 0)

            env = env_path.read_text(encoding="utf-8")
            self.assertIn("SLACK_BOT_TOKEN=xoxb-retained-token", env)
            self.assertIn("SLACK_APP_TOKEN=xapp-retained-token", env)
            self.assertIn("CUSTOM_SETTING=retained", env)
            identity = identity_path.read_text(encoding="utf-8")
            self.assertIn("- Role: Custom role", identity)
            self.assertIn("- Tone: Custom tone", identity)
            self.assertIn("- Company: Acme", identity)
            self.assertIn("- Name: Bridge Two", identity)

    def test_preview_does_not_write(self):
        directory, root = self.project()
        with directory, self.configured_root(root):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(CONFIGURE.main([]), 0)
                self.assertEqual(
                    CONFIGURE.main(
                        ["--company", "Preview Co.", "--bot-name", "Preview Bot"]
                    ),
                    0,
                )
            self.assertFalse((root / ".env").exists())
            self.assertFalse((root / "IDENTITY.md").exists())
            self.assertFalse((root / "config" / "slack-app-manifest.yaml").exists())
            self.assertIn("preview", output.getvalue())
            self.assertIn("would create 5 files", output.getvalue())
            self.assertIn("would update 3 files", output.getvalue())
            self.assertIn('SLACK_BRIDGE_COMPANY: "" -> "Preview Co."', output.getvalue())

    def test_interactive_wizard_saves_manifest_before_hidden_token_entry(self):
        directory, root = self.project()
        with directory, self.configured_root(root):
            output = io.StringIO()
            def token_input(_prompt: str) -> str:
                self.assertTrue((root / "config" / "slack-app-manifest.yaml").is_file())
                return ""

            with patch("builtins.input", side_effect=["Acme", "Acme Bridge", "codex", ""]), patch.object(
                sys.stdin, "isatty", return_value=True
            ), patch.object(CONFIGURE.getpass, "getpass", side_effect=token_input), contextlib.redirect_stdout(output):
                self.assertEqual(CONFIGURE.main(["--interactive", "--apply"]), 0)
            self.assertIn("Create and install the Slack app", output.getvalue())
            self.assertIn(
                'name: "Acme Bridge"',
                (root / "config" / "slack-app-manifest.yaml").read_text(encoding="utf-8"),
            )

    def test_token_entry_masks_output_and_blank_keeps_existing_tokens(self):
        directory, root = self.project()
        bot_token = "xoxb-test-token"
        app_token = "xapp-test-token"
        with directory, self.configured_root(root):
            output = io.StringIO()
            with patch.object(sys.stdin, "isatty", return_value=True), patch.object(
                CONFIGURE.getpass, "getpass", side_effect=[bot_token, app_token]
            ), contextlib.redirect_stdout(output):
                self.assertEqual(CONFIGURE.main(["--tokens", "--apply"]), 0)
            self.assertNotIn(bot_token, output.getvalue())
            self.assertNotIn(app_token, output.getvalue())

            env_path = root / ".env"
            before = env_path.read_text(encoding="utf-8")
            os.chmod(env_path, 0o644)
            with patch.object(sys.stdin, "isatty", return_value=True), patch.object(
                CONFIGURE.getpass, "getpass", side_effect=["xoxb-replaced-token", "xapp-replaced-token"]
            ):
                self.assertEqual(CONFIGURE.main(["--tokens", "--apply"]), 0)
            self.assertNotEqual(env_path.read_text(encoding="utf-8"), before)
            self.assertEqual(env_path.stat().st_mode & 0o777, 0o600)

            before = env_path.read_text(encoding="utf-8")
            with patch.object(sys.stdin, "isatty", return_value=True), patch.object(
                CONFIGURE.getpass, "getpass", side_effect=["", ""]
            ):
                self.assertEqual(CONFIGURE.main(["--tokens", "--apply"]), 0)
            self.assertEqual(env_path.read_text(encoding="utf-8"), before)

    def test_token_entry_requires_an_interactive_terminal(self):
        directory, root = self.project()
        with directory, self.configured_root(root), patch.object(
            sys.stdin, "isatty", return_value=False
        ):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(CONFIGURE.main(["--tokens", "--apply"]), 2)
            self.assertIn("interactive terminal", stderr.getvalue())
            self.assertFalse((root / ".env").exists())

    def test_token_entry_rejects_getpass_warning_without_reading_a_token(self):
        directory, root = self.project()
        with directory, self.configured_root(root), patch.object(
            sys.stdin, "isatty", return_value=True
        ):
            stderr = io.StringIO()

            def warning_input(_prompt: str) -> str:
                warnings.warn(
                    "echo control failed", CONFIGURE.getpass.GetPassWarning, stacklevel=2
                )
                return "unreachable"

            with patch.object(CONFIGURE.getpass, "getpass", side_effect=warning_input), contextlib.redirect_stderr(stderr):
                self.assertEqual(CONFIGURE.main(["--tokens", "--apply"]), 2)
            self.assertIn("echo-free local terminal", stderr.getvalue())
            self.assertFalse((root / ".env").exists())

    def test_invalid_token_is_rejected_without_echoing_it(self):
        directory, root = self.project()
        invalid_token = "not-a-slack-token"
        with directory, self.configured_root(root), patch.object(
            sys.stdin, "isatty", return_value=True
        ):
            stderr = io.StringIO()
            with patch.object(
                CONFIGURE.getpass, "getpass", side_effect=[invalid_token, ""]
            ), contextlib.redirect_stderr(stderr):
                self.assertEqual(CONFIGURE.main(["--tokens", "--apply"]), 2)
            self.assertNotIn(invalid_token, stderr.getvalue())
            self.assertFalse((root / ".env").exists())

    def test_cli_errors_hide_values_and_company_can_be_cleared(self):
        directory, root = self.project()
        secret_like_value = "xoxb-should-not-appear"
        with directory, self.configured_root(root):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as error:
                CONFIGURE.main(["--unknown-token", secret_like_value])
            self.assertEqual(error.exception.code, 2)
            self.assertNotIn(secret_like_value, stderr.getvalue())

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(CONFIGURE.main(["--bot-name", "   ", "--apply"]), 2)
            self.assertIn("bot name must not be blank", stderr.getvalue())

            self.assertEqual(
                CONFIGURE.main(["--company", "Acme", "--bot-name", "Bridge", "--apply"]),
                0,
            )
            self.assertEqual(CONFIGURE.main(["--company", "", "--apply"]), 0)
            self.assertIn(
                'SLACK_BRIDGE_COMPANY=""',
                (root / ".env").read_text(encoding="utf-8"),
            )
            self.assertIn("- Company: \n", (root / "IDENTITY.md").read_text(encoding="utf-8"))

    def test_escaping_and_effective_first_wins_config(self):
        directory, root = self.project()
        with directory, self.configured_root(root):
            (root / ".env.local").write_text(
                "AI_WORKER_PROVIDER=claude\n", encoding="utf-8"
            )
            self.assertEqual(
                CONFIGURE.main(
                    [
                        "--company",
                        'Acme "#1',
                        "--bot-name",
                        'Bridge: "One"',
                        "--provider",
                        "codex",
                        "--apply",
                    ]
                ),
                0,
            )
            env = (root / ".env").read_text(encoding="utf-8")
            manifest = (root / "config" / "slack-app-manifest.yaml").read_text(
                encoding="utf-8"
            )
            self.assertIn('SLACK_BRIDGE_COMPANY="Acme \\"#1"', env)
            self.assertIn('name: "Bridge: \\"One\\""', manifest)
            effective, sources = CONFIGURE.effective_settings(root, os.environ)
            self.assertEqual(effective["AI_WORKER_PROVIDER"], "codex")
            self.assertEqual(sources["AI_WORKER_PROVIDER"], ".env")

    def test_tokens_resume_from_env_local_without_overriding_effective_settings(self):
        directory, root = self.project()
        local_settings = {
            "SLACK_BRIDGE_PROFILE": "public",
            "SLACK_BRIDGE_HOOK_PORT": "9123",
            "SLACK_BRIDGE_COMPANY": "Local Co.",
            "SLACK_BRIDGE_BOT_NAME": "Local Bridge",
            "SLACK_BRIDGE_AUTH_TOKEN": "local-auth-token",
            "SLACK_BOT_TOKEN": "xoxb-local-token",
            "SLACK_APP_TOKEN": "xapp-local-token",
            "AI_WORKER_PROVIDER": "codex",
            "SLACK_BRIDGE_DATA_ROOT": "/tmp/local-bridge-data",
            "CUSTOM_SETTING": "retained",
        }
        with directory, self.configured_root(root):
            (root / ".env.local").write_text(
                "".join(f"{key}={value}\n" for key, value in local_settings.items()),
                encoding="utf-8",
            )
            with patch.object(sys.stdin, "isatty", return_value=True), patch.object(
                CONFIGURE.getpass, "getpass", side_effect=["", ""]
            ):
                self.assertEqual(CONFIGURE.main(["--tokens", "--apply"]), 0)
            runtime = self.runtime_values(root, tuple(local_settings))
            self.assertEqual(runtime, local_settings)
            self.assertIn(
                'name: "Local Bridge"',
                (root / "config" / "slack-app-manifest.yaml").read_text(encoding="utf-8"),
            )

    def test_interactive_blank_values_keep_env_local_runtime_settings(self):
        directory, root = self.project()
        local_settings = {
            "SLACK_BRIDGE_PROFILE": "public",
            "SLACK_BRIDGE_COMPANY": "Local Co.",
            "SLACK_BRIDGE_BOT_NAME": "Local Bridge",
            "SLACK_BRIDGE_AUTH_TOKEN": "local-auth-token",
            "SLACK_BOT_TOKEN": "xoxb-local-token",
            "SLACK_APP_TOKEN": "xapp-local-token",
            "AI_WORKER_PROVIDER": "codex",
            "SLACK_BRIDGE_DATA_ROOT": "/tmp/local-bridge-data",
            "CUSTOM_SETTING": "retained",
        }
        with directory, self.configured_root(root):
            (root / ".env.local").write_text(
                "".join(f"{key}={value}\n" for key, value in local_settings.items()),
                encoding="utf-8",
            )
            with patch("builtins.input", side_effect=["", "", "", ""]), patch.object(
                sys.stdin, "isatty", return_value=True
            ), patch.object(CONFIGURE.getpass, "getpass", side_effect=["", ""]):
                self.assertEqual(CONFIGURE.main(["--interactive", "--apply"]), 0)
            self.assertEqual(self.runtime_values(root, tuple(local_settings)), local_settings)

    def test_process_environment_conflict_stops_apply_before_writing(self):
        directory, root = self.project()
        with directory, self.configured_root(root), patch.dict(
            os.environ, {"AI_WORKER_PROVIDER": "claude"}, clear=True
        ):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(
                    CONFIGURE.main(["--provider", "codex", "--apply"]), 2
                )
            self.assertFalse((root / ".env").exists())
            self.assertIn("process environment", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
