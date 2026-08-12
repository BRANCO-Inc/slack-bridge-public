import unittest
from types import SimpleNamespace

import session_lifecycle
from hook_server import HookHandler
from session import Session, Status
from session_lifecycle import SessionLifecycle


class CallerRetryTests(unittest.TestCase):
    def test_session_failure_posts_once(self):
        calls = []

        def post_to_slack(channel_id, thread_ts, text):
            calls.append((channel_id, thread_ts, text))
            return {"ok": False, "error": "missing_scope"}

        session = Session(
            channel_id="C1",
            thread_ts="1.0",
            case_id="case-1",
            conversation_identity="T1:C1:1.0",
            status=Status.BUSY,
        )

        def update_fields(_identity, updates):
            for key, value in updates.items():
                setattr(session, key, value)
            return session

        lifecycle = object.__new__(SessionLifecycle)
        lifecycle.tmux = SimpleNamespace()
        lifecycle.store = SimpleNamespace(update_fields=update_fields)
        lifecycle.input_detector = SimpleNamespace(clear_session=lambda _thread_ts: None)
        lifecycle.finalize_session_turns = lambda *_args, **_kwargs: None
        lifecycle.post_to_slack = post_to_slack

        result = lifecycle._soft_fail_transition(session, "worker_failed")

        self.assertIs(result, session)
        self.assertEqual(calls, [("C1", "1.0", session_lifecycle.SESSION_ABNORMAL_EXIT_TEXT)])

    def test_reply_failure_calls_slack_once(self):
        calls = []

        def post_to_slack(*args, **kwargs):
            calls.append((args, kwargs))
            return {"ok": False, "error": "missing_scope"}

        handler = object.__new__(HookHandler)
        handler.bridge = SimpleNamespace(post_to_slack=post_to_slack)

        result = handler._post_reply("C1", "1.0", "reply", substantive=True)

        self.assertEqual(result, {"ok": False, "error": "missing_scope"})
        self.assertEqual(len(calls), 1)

    def test_reply_preserves_partial_post_read_back(self):
        partial_result = {
            "ok": False,
            "error": "missing_scope",
            "posted_reply_ts": ["1.1"],
        }
        handler = object.__new__(HookHandler)
        handler.bridge = SimpleNamespace(post_to_slack=lambda *_args, **_kwargs: partial_result)

        result = handler._post_reply("C1", "1.0", "reply", substantive=True)

        self.assertIs(result, partial_result)

    def test_reaction_failure_calls_slack_once(self):
        calls = []

        def add_reaction(channel_id, message_ts, reaction_name):
            calls.append((channel_id, message_ts, reaction_name))
            return {"ok": False, "error": "missing_scope"}

        handler = object.__new__(HookHandler)
        handler.bridge = SimpleNamespace(add_reaction=add_reaction)

        result = handler._post_reaction("C1", "1.0", "eyes")

        self.assertEqual(result, {"ok": False, "error": "missing_scope"})
        self.assertEqual(calls, [("C1", "1.0", "eyes")])

if __name__ == "__main__":
    unittest.main()
