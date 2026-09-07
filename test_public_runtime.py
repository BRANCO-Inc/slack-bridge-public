import os
import secrets
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

if "slack_sdk" not in sys.modules:
    slack_sdk = types.ModuleType("slack_sdk")
    slack_sdk.WebClient = object
    slack_errors = types.ModuleType("slack_sdk.errors")

    class SlackApiError(Exception):
        pass

    slack_errors.SlackApiError = SlackApiError
    slack_bolt = types.ModuleType("slack_bolt")
    slack_bolt.App = object
    socket_mode = types.ModuleType("slack_bolt.adapter.socket_mode")
    socket_mode.SocketModeHandler = object
    sys.modules.update(
        {
            "slack_sdk": slack_sdk,
            "slack_sdk.errors": slack_errors,
            "slack_bolt": slack_bolt,
            "slack_bolt.adapter": types.ModuleType("slack_bolt.adapter"),
            "slack_bolt.adapter.socket_mode": socket_mode,
        }
    )

import config
import envelope
import runtime_auth
import slack_api
import slack_bridge
import turn_context
from bridge_state import BridgeState
from hook_server import HookBridge, HookHandler
from scripts import doctor
from session import Session, Status
from session_store import SessionStore
from tmux_gateway import BRIDGE_PANE_INSTANCE, TmuxGateway, TmuxQueryError


def make_session() -> Session:
    return Session.create(
        thread_ts="1.0",
        conversation_identity="T1:C1:1.0",
        channel_id="C1",
        team_id="T1",
        user_id="U1",
        call_name="owner",
        window_name="worker",
        case_id="case-1",
    )


class InlineThread:
    def __init__(self, *, target, args=(), kwargs=None, daemon=False):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


class DoctorTests(unittest.TestCase):
    def test_data_root_check_does_not_create_a_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "not-created"
            with patch.dict(os.environ, {"SLACK_BRIDGE_DATA_ROOT": str(target)}):
                result = doctor.check_data_root()
            self.assertTrue(result.ok)
            self.assertFalse(target.exists())


class DurableInitialQueueTests(unittest.TestCase):
    def test_initial_queue_is_resumed_with_its_claim_id(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(str(Path(directory) / "state.sqlite3"))
            session = make_session()
            payload = {"event_id": "event-1", "source_message": {"ts": "1.0"}}
            queue_id = store.create_initial_queue_if_absent(
                session, payload, lease_owner="test-initial"
            )
            self.assertIsNotNone(queue_id)
            self.assertEqual(store.state.reset_dispatching_event_queue_leases(reason="restart"), 1)

            started = []
            worker = SimpleNamespace(start=lambda *args, **kwargs: started.append((args, kwargs)))
            runtime = SimpleNamespace(
                state=store.state,
                store=store,
                worker=worker,
                post_to_slack=lambda *_args, **_kwargs: {"ok": True},
            )
            with patch.object(slack_bridge.threading, "Thread", InlineThread):
                stats = slack_bridge.drain_durable_event_queue(runtime)

            self.assertEqual(stats["processed"], 1)
            self.assertEqual(started[0][1]["queue_id"], queue_id)


class ReplyAuthTests(unittest.TestCase):
    def test_turn_wrapper_uses_private_runtime_auth_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            original_tmp = config.TMP_DIR
            original_auth = config.WORKER_REPLY_AUTH_WRAPPER_PATH
            original_artifacts = turn_context.TURN_ARTIFACTS_BASE
            token = secrets.token_urlsafe(24)
            try:
                config.TMP_DIR = directory
                config.SLACK_BRIDGE_AUTH_TOKEN = token
                config.WORKER_REPLY_AUTH_WRAPPER_PATH = ""
                turn_context.TURN_ARTIFACTS_BASE = str(Path(directory) / "turns")
                auth_path = runtime_auth.prepare_worker_reply_auth()
                artifacts = turn_context.write_turn_context(make_session(), turn_id="turn-1")
                wrapper_text = Path(artifacts.reply_command_path).read_text(encoding="utf-8")
                self.assertIn(str(auth_path), wrapper_text)
                self.assertNotIn(token, wrapper_text)
                self.assertEqual(stat.S_IMODE(os.stat(auth_path).st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(os.stat(artifacts.reply_command_path).st_mode), 0o700)
            finally:
                config.TMP_DIR = original_tmp
                config.WORKER_REPLY_AUTH_WRAPPER_PATH = original_auth
                turn_context.TURN_ARTIFACTS_BASE = original_artifacts


class DeliveryUnknownTests(unittest.TestCase):
    def test_transport_failure_is_terminal_posted_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(str(Path(directory) / "state.sqlite3"))
            state = BridgeState(store.db_path)
            session = make_session()
            session.status = Status.BUSY
            session.worker_session_id = "worker-1"
            session.pane_id = "%1"
            session.active_turn_id = "turn-1"
            store.save(session)
            state.open_turn_attempt(
                "turn-1",
                channel_id=session.channel_id,
                thread_ts=session.thread_ts,
                event_id="event-1",
            )

            post = slack_api.make_post_to_slack(
                SimpleNamespace(
                    chat_postMessage=lambda **_kwargs: (_ for _ in ()).throw(ConnectionError())
                ),
                store,
                state,
                conversation_identity_for=lambda channel_id, thread_ts: (
                    f"T1:{channel_id}:{thread_ts}"
                ),
            )
            captured = []
            handler = object.__new__(HookHandler)
            handler.bridge = HookBridge(
                store=store,
                state=state,
                post_to_slack=post,
                handle_turn_complete=lambda **_kwargs: "idle",
            )
            handler._json = lambda code, payload, headers=None: captured.append((code, payload))
            handler._handle_case_reply(
                {
                    "reply_request_id": "reply-1",
                    "case_id": session.case_id,
                    "turn_id": "turn-1",
                    "text": "reply",
                    "completion_action": "done",
                    "session_id": "worker-1",
                    "pane_id": "%1",
                    "window_name": "worker",
                }
            )
            self.assertEqual(captured[0][0], 202)
            self.assertEqual(state.get_reply("reply-1")["status"], "posted_unknown")
            replay = state.claim_reply(
                "reply-1", case_id=session.case_id, payload={"text": "reply"}
            )
            self.assertFalse(replay.claimed)

    def test_transient_channel_info_failure_is_retryable(self):
        calls = []

        def conversations_info(**_kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise ConnectionError()
            return {"channel": {"id": "C1", "name": "general"}}

        client = SimpleNamespace(conversations_info=conversations_info)
        runtime = SimpleNamespace(user_client=client, app_client=client)
        with patch.object(slack_api.time, "sleep", return_value=None):
            envelope.fetch_channel_info(
                runtime,
                {},
                {"channel": "C1", "channel_type": "channel"},
            )
        self.assertEqual(len(calls), 2)


class TmuxAndControlTests(unittest.TestCase):
    def test_detached_tmux_size_query_targets_the_worker_window(self):
        gateway = object.__new__(TmuxGateway)
        gateway.session_name = "slack-bridge"
        commands = []
        gateway._run = lambda args, **_kwargs: commands.append(args) or "120 40"

        gateway._target_pool_window_size(config.GENERAL_WINDOW_NAME)

        self.assertIn("-t", commands[0])
        self.assertIn("#{window_width} #{window_height}", commands[0])

    def test_new_tmux_session_marks_its_bootstrap_pane_reusable(self):
        gateway = object.__new__(TmuxGateway)
        gateway.session_name = "slack-bridge"
        session_states = iter((False, True))
        gateway._session_exists = lambda: next(session_states)
        gateway._run = lambda *_args, **_kwargs: "%1"
        marked = []
        gateway.mark_bridge_pane = marked.append

        gateway.ensure_session()

        self.assertEqual(marked, ["%1"])

    def test_killing_the_last_worker_recreates_its_pool_window(self):
        gateway = object.__new__(TmuxGateway)
        gateway._pane_window_and_ownership = lambda _pane_id: (
            config.GENERAL_WINDOW_NAME,
            "1",
            BRIDGE_PANE_INSTANCE,
        )
        gateway._run = lambda *_args, **_kwargs: ""
        gateway.pane_exists = lambda _pane_id: False
        ensured = []
        gateway.ensure_window = ensured.append
        gateway._rebalance_window_layout = lambda _window_name: None

        self.assertTrue(gateway.kill_pane("%1"))
        self.assertEqual(ensured, [config.GENERAL_WINDOW_NAME])

    def test_tmux_query_error_is_not_treated_as_missing_pane(self):
        gateway = object.__new__(TmuxGateway)
        gateway._run = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("permission denied")
        )
        with self.assertRaises(TmuxQueryError):
            gateway.pane_exists("%1")

    def test_natural_language_control_text_is_dispatched_to_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(str(Path(directory) / "state.sqlite3"))
            state = BridgeState(store.db_path)
            session = make_session()
            session.status = Status.IDLE
            store.save(session)
            routed = []
            runtime = SimpleNamespace(
                store=store,
                state=state,
                dispatch=SimpleNamespace(route=lambda *args: routed.append(args)),
                post_to_slack=lambda *_args, **_kwargs: {"ok": True},
            )
            envelope = {
                "event_id": "event-control-text",
                "conversation_identity": session.conversation_identity,
                "normalized_event_type": "thread_message",
                "source_message": {"ts": "1.1", "text": "終了"},
                "channel": {"id": session.channel_id},
                "actor": {"user_id": session.user_id},
                "reply_target": {"channel_id": session.channel_id, "thread_ts": session.thread_ts},
            }
            state.claim_event("event-control-text", source_event_type="message")
            slack_bridge.route_envelope(runtime, envelope)
            self.assertEqual(len(routed), 1)

    def test_unauthorized_block_control_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(str(Path(directory) / "state.sqlite3"))
            state = BridgeState(store.db_path)
            session = make_session()
            store.save(session)
            runtime = SimpleNamespace(
                store=store,
                state=state,
                lifecycle=SimpleNamespace(
                    kill=lambda *_args, **_kwargs: self.fail("must not kill")
                ),
            )
            slack_bridge.handle_block_action(
                runtime,
                {
                    "event_id": "block-control-1",
                    "actions": [{"action_id": "session_end", "action_ts": "1.0"}],
                    "channel": {"id": "C1"},
                    "message": {"ts": "1.0"},
                    "user": {"id": "U-other"},
                    "trigger_id": "trigger-1",
                },
            )
            rows = [row for row in [state.get_event("block-control-1")] if row]
            self.assertEqual(rows[0]["failure_reason"], "block_control_unauthorized")


if __name__ == "__main__":
    unittest.main()
