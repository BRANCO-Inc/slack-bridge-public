import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import config
import extensions
from message_dispatch import MessageDispatch
from pane_pool import PANE_POOLS, all_window_names, pool_for_envelope
from runtime_env import resolve_ai_worker_provider
from session import Session, Status
from session_store import SessionStore


def make_session() -> Session:
    session = Session.create(
        thread_ts="1.0",
        conversation_identity="T1:C1:1.0",
        channel_id="C1",
        team_id="T1",
        user_id="U1",
        call_name="owner",
        window_name=config.GENERAL_WINDOW_NAME,
    )
    session.status = Status.BUSY
    return session


class CoreDistributionTests(unittest.TestCase):
    def test_ai_boot_remains_a_core_extension(self):
        event = {"normalized_event_type": "ai_boot_reaction"}

        self.assertEqual([extension.name for extension in extensions.EXTENSIONS], ["ai_boot"])
        self.assertTrue(extensions.EXTENSIONS[0].match_envelope(event))
        self.assertEqual(pool_for_envelope(event), config.GENERAL_PANE_POOL_NAME)

    def test_generic_message_is_persisted_in_the_existing_session_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(str(Path(directory) / "state.sqlite3"))
            session = make_session()
            store.save(session)
            dispatch = MessageDispatch(
                SimpleNamespace(),
                store,
                SimpleNamespace(),
                SimpleNamespace(),
                lambda *_args, **_kwargs: {"ok": True},
                lambda *_args, **_kwargs: None,
            )
            payload = {"normalized_event_type": "thread_message", "source_message": {"text": "続けて"}}

            dispatch.route(session.conversation_identity, payload, lambda _text: None)

            self.assertEqual(store.load(session.conversation_identity).event_queue, [payload])

    def test_public_runtime_uses_only_the_general_pool(self):
        self.assertEqual(set(PANE_POOLS), {config.GENERAL_PANE_POOL_NAME})
        self.assertEqual(all_window_names(), list(config.GENERAL_PANE_POOL_WINDOWS))
        self.assertEqual(config.PANE_POOL_MAX_PANES, config.GENERAL_PANE_POOL_MAX_PANES)

    def test_claude_is_default_and_codex_is_selectable(self):
        self.assertEqual(resolve_ai_worker_provider({}), "claude")
        self.assertEqual(resolve_ai_worker_provider({"AI_WORKER_PROVIDER": "codex"}), "codex")


if __name__ == "__main__":
    unittest.main()
