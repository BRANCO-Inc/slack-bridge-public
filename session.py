from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


class Status(Enum):
    CREATING = "creating"
    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"
    IDLE = "idle"
    WAITING = "waiting_for_input"
    SUSPENDED = "suspended"
    ERROR = "error"
    KILLED = "killed"


ACTIVE = {
    Status.CREATING,
    Status.STARTING,
    Status.READY,
    Status.BUSY,
    Status.IDLE,
    Status.WAITING,
}
ACTIVE_VALUES = frozenset(status.value for status in ACTIVE)

ALLOWED_TRANSITIONS: dict[Status, set[Status]] = {
    Status.CREATING: {Status.STARTING, Status.SUSPENDED, Status.ERROR, Status.KILLED},
    Status.STARTING: {Status.READY, Status.BUSY, Status.SUSPENDED, Status.ERROR, Status.KILLED},
    Status.READY: {Status.BUSY, Status.SUSPENDED, Status.KILLED, Status.ERROR},
    Status.BUSY: {Status.IDLE, Status.WAITING, Status.SUSPENDED, Status.KILLED, Status.ERROR},
    Status.IDLE: {Status.BUSY, Status.SUSPENDED, Status.KILLED, Status.ERROR},
    Status.WAITING: {Status.BUSY, Status.IDLE, Status.SUSPENDED, Status.KILLED, Status.ERROR},
    Status.SUSPENDED: {Status.STARTING, Status.KILLED, Status.ERROR},
    Status.ERROR: set(),
    Status.KILLED: {Status.STARTING},
}


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def coerce_status(status: Status | str) -> Status:
    if isinstance(status, Status):
        return status
    if hasattr(status, "value"):
        status = status.value
    return Status(status)


def coerce_event_queue(value) -> list[dict | str]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    return [value]


def _parse_conversation_identity(value: str) -> tuple[str, str, str]:
    parts = value.split(":", 2)
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    return "", "", value


def _json_dumps(value: dict | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_loads(value: str | None) -> dict | None:
    if not value:
        return None
    return json.loads(value)


def activate_turn_fields(
    session: Session,
    *,
    turn_id: str,
    event_id: str | None,
    owner_user_id: str | None,
    queue_id: int | None = None,
) -> dict:
    started_at = now_utc()
    return {
        "last_activity_at": started_at,
        "active_turn_id": turn_id,
        "active_turn_event_id": event_id,
        "active_turn_queue_id": queue_id,
        "active_turn_owner_user_id": owner_user_id or session.user_id,
        "active_turn_started_at": started_at,
    }


def clear_turn_fields() -> dict:
    return {
        "active_turn_id": None,
        "active_turn_event_id": None,
        "active_turn_queue_id": None,
        "active_turn_owner_user_id": None,
        "active_turn_started_at": None,
    }


@dataclass(slots=True)
class Session:
    channel_id: str
    thread_ts: str
    case_id: str
    conversation_identity: str = ""
    user_id: str = ""
    call_name: str = ""
    team_id: str = ""
    channel_name: str = ""
    channel_type: str = "channel"
    is_external_shared: bool = False
    window_name: str = ""
    pane_id: str | None = None
    worker_session_id: str | None = None
    status: Status = Status.CREATING
    failure_reason: str | None = None
    active_turn_id: str | None = None
    active_turn_event_id: str | None = None
    active_turn_queue_id: int | None = None
    active_turn_owner_user_id: str | None = None
    active_turn_started_at: str | None = None
    input_wait_json: str | None = None
    last_reply_request_id: str | None = None
    last_activity_at: str = field(default_factory=now_utc)
    last_reply_ts: str | None = None
    last_substantive_reply_ts: str | None = None
    last_thread_message_ts: str | None = None
    last_event_id: str | None = None
    last_event_ts: str | None = None
    created_at: str = field(default_factory=now_utc)
    event_queue: list[dict | str] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self.status = coerce_status(self.status)
        self.is_external_shared = bool(self.is_external_shared)
        self.event_queue = coerce_event_queue(self.event_queue)
        if self.thread_ts == "" and self.conversation_identity:
            _, _, thread_ts = _parse_conversation_identity(self.conversation_identity)
            self.thread_ts = thread_ts
        if not self.conversation_identity and self.team_id and self.channel_id and self.thread_ts:
            self.conversation_identity = f"{self.team_id}:{self.channel_id}:{self.thread_ts}"
        if self.conversation_identity:
            team_id, channel_id, thread_ts = _parse_conversation_identity(
                self.conversation_identity
            )
            if not self.team_id and team_id:
                self.team_id = team_id
            if not self.channel_id and channel_id:
                self.channel_id = channel_id
            if not self.thread_ts and thread_ts:
                self.thread_ts = thread_ts

    @property
    def input_wait(self) -> dict | None:
        return _json_loads(self.input_wait_json)

    @input_wait.setter
    def input_wait(self, value: dict | None) -> None:
        self.input_wait_json = _json_dumps(value)

    @classmethod
    def create(
        cls,
        thread_ts: str | None = None,
        user_id: str = "",
        call_name: str = "",
        window_name: str = "",
        case_id: str = "",
        *,
        conversation_identity: str | None = None,
        channel_id: str = "",
        team_id: str = "",
        channel_name: str = "",
        channel_type: str = "channel",
        is_external_shared: bool = False,
    ):
        identity = conversation_identity or thread_ts
        if not identity:
            raise TypeError(
                "Session.create() requires either 'conversation_identity' or 'thread_ts'"
            )
        if thread_ts:
            resolved_thread_ts = thread_ts
        else:
            _, _, resolved_thread_ts = _parse_conversation_identity(identity)
        return cls(
            channel_id=channel_id,
            thread_ts=resolved_thread_ts,
            case_id=case_id,
            conversation_identity=identity,
            user_id=user_id,
            call_name=call_name,
            team_id=team_id,
            channel_name=channel_name,
            channel_type=channel_type,
            is_external_shared=is_external_shared,
            window_name=window_name,
        )

    @classmethod
    def from_record(cls, record: dict):
        conversation_identity = record.get("conversation_identity", "")
        thread_ts = record.get("thread_ts") or ""
        if not thread_ts and conversation_identity:
            _, _, thread_ts = _parse_conversation_identity(conversation_identity)
        input_wait_json = record.get("input_wait_json")
        return cls(
            channel_id=record.get("channel_id", ""),
            thread_ts=thread_ts,
            case_id=record.get("case_id", ""),
            conversation_identity=conversation_identity,
            user_id=record.get("user_id", ""),
            call_name=record.get("call_name", ""),
            team_id=record.get("team_id", ""),
            channel_name=record.get("channel_name", ""),
            channel_type=record.get("channel_type", "channel"),
            is_external_shared=record.get("is_external_shared", False),
            window_name=record.get("window_name", ""),
            pane_id=record.get("pane_id"),
            worker_session_id=record.get("worker_session_id"),
            status=record.get("status", Status.ERROR.value),
            failure_reason=record.get("failure_reason"),
            active_turn_id=record.get("active_turn_id"),
            active_turn_event_id=record.get("active_turn_event_id"),
            active_turn_queue_id=record.get("active_turn_queue_id"),
            active_turn_owner_user_id=record.get("active_turn_owner_user_id"),
            active_turn_started_at=record.get("active_turn_started_at"),
            input_wait_json=input_wait_json,
            last_reply_request_id=record.get("last_reply_request_id"),
            last_activity_at=record.get("last_activity_at", now_utc()),
            last_reply_ts=record.get("last_reply_ts"),
            last_substantive_reply_ts=record.get(
                "last_substantive_reply_ts", record.get("last_reply_ts")
            ),
            last_thread_message_ts=record.get(
                "last_thread_message_ts", thread_ts or record.get("last_event_ts")
            ),
            last_event_id=record.get("last_event_id"),
            last_event_ts=record.get("last_event_ts"),
            created_at=record.get("created_at", now_utc()),
            event_queue=coerce_event_queue(record.get("event_queue", [])),
        )

    def to_db_record(self) -> dict:
        return {
            "channel_id": self.channel_id,
            "thread_ts": self.thread_ts,
            "case_id": self.case_id,
            "worker_session_id": self.worker_session_id,
            "status": self.status.value,
            "failure_reason": self.failure_reason,
            "user_id": self.user_id,
            "call_name": self.call_name,
            "team_id": self.team_id,
            "channel_name": self.channel_name,
            "channel_type": self.channel_type,
            "is_external_shared": int(self.is_external_shared),
            "window_name": self.window_name,
            "pane_id": self.pane_id,
            "active_turn_id": self.active_turn_id,
            "active_turn_event_id": self.active_turn_event_id,
            "active_turn_queue_id": self.active_turn_queue_id,
            "active_turn_owner_user_id": self.active_turn_owner_user_id,
            "active_turn_started_at": self.active_turn_started_at,
            "input_wait_json": self.input_wait_json,
            "last_reply_request_id": self.last_reply_request_id,
            "last_activity_at": self.last_activity_at,
            "last_reply_ts": self.last_reply_ts,
            "last_substantive_reply_ts": self.last_substantive_reply_ts,
            "last_thread_message_ts": self.last_thread_message_ts,
            "last_event_id": self.last_event_id,
            "last_event_ts": self.last_event_ts,
            "created_at": self.created_at,
        }

    def to_record(self) -> dict:
        record = self.to_db_record()
        record.update(
            {
                "conversation_identity": self.conversation_identity,
                "event_queue": list(self.event_queue),
            }
        )
        return record

    def touch(self):
        self.last_activity_at = now_utc()

    def activate_turn(
        self, *, turn_id: str, event_id: str | None = None, owner_user_id: str | None = None
    ):
        self.active_turn_id = turn_id
        self.active_turn_event_id = event_id
        self.active_turn_owner_user_id = owner_user_id or self.user_id
        self.active_turn_started_at = now_utc()

    def clear_active_turn(self):
        self.active_turn_id = None
        self.active_turn_event_id = None
        self.active_turn_queue_id = None
        self.active_turn_owner_user_id = None
        self.active_turn_started_at = None

    def is_active(self) -> bool:
        return self.status in ACTIVE
