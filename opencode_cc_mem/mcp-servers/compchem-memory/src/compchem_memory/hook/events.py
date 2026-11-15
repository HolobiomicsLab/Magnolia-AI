"""The one event model every client adapter produces.

A client hook payload (Claude Code, OpenCode, later Codex and Vibe) is
translated by its adapter into a ``HookEvent``; nothing downstream sees the
native payload. Two consequences worth stating:

* ``cwd`` is the raw location the client reported, not a project. Binding it
  to a resolved project is ``ProjectPaths``' job (task 2), so a hook can never
  mistake the client's working directory for the project it belongs to.
* ``ToolCall.output`` is text or ``None``. Adapters call ``normalise_output``
  before building the call, so a native list of content blocks or a JSON
  object does not leak into the transcript or the critic.

Tool names arrive in several shapes. ``canonical_tool`` reduces them to
``(server, name)`` so the policy layer can match one spelling per tool. Only
the forms observed in the task 0 probes are handled here; Codex and Vibe forms
are added with their probe report rather than guessed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

SCHEMA_VERSION = 1

EVENTS = (
    "session-start",
    "prompt",
    "pre-tool",
    "post-tool",
    "turn-end",
    "session-end",
    "agent-start",
    "agent-end",
)

# Servers whose OpenCode tool names are ``<server>_<tool>``. A bare underscore
# is not enough evidence of a server: an unknown prefix stays a built-in name.
_KNOWN_SERVERS = ("compchem-tools", "compchem-memory", "perspicacite")
_MCP_PREFIX = "mcp__"


def canonical_tool(raw: str) -> tuple[str | None, str]:
    """Return ``(server, name)`` for a client's raw tool name.

    Handles Claude Code's ``mcp__<server>__<tool>``, OpenCode's
    ``<server>_<tool>`` for the known servers, and built-in names, which are
    lower-cased with ``server=None`` (clients differ in their casing:
    ``Edit``/``edit``).
    """
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"tool name must be a non-empty string, got {raw!r}")

    if raw.startswith(_MCP_PREFIX):
        server, separator, tool = raw[len(_MCP_PREFIX):].partition("__")
        if separator and server and tool:
            return server, tool

    for server in _KNOWN_SERVERS:
        prefix = f"{server}_"
        if raw.startswith(prefix) and raw[len(prefix):]:
            return server, raw[len(prefix):]

    return None, raw.lower()


def normalise_output(value: object) -> str | None:
    """Reduce a native tool output to text.

    A list is treated as content blocks: the ``text`` of each block that has
    one, joined by a newline (images and other blocks carry no text and are
    dropped). Any other JSON value is serialised compactly but with its
    non-ASCII characters intact.
    """
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, list):
        texts = [text for text in (_block_text(block) for block in value) if text is not None]
        return "\n".join(texts)
    return json.dumps(value, ensure_ascii=False)


def _block_text(block: object) -> str | None:
    """Return a content block's ``text``, whether it is a dict or an object."""
    if isinstance(block, dict):
        text = block.get("text")
    else:
        text = getattr(block, "text", None)
    return text if isinstance(text, str) else None


@dataclass
class ToolCall:
    """One tool invocation, with ``output`` already reduced to text."""

    name: str
    server: str | None
    raw_name: str
    call_id: str | None
    input: dict
    output: str | None = None
    is_error: bool = False


@dataclass
class AgentRef:
    """A subagent; ``HookEvent.agent=None`` means the main agent."""

    id: str
    type: str | None = None


@dataclass
class HookEvent:
    """A client event, independent of the client that produced it."""

    harness: str
    event: str
    session_id: str
    cwd: str
    schema_version: int = SCHEMA_VERSION
    turn_id: str | None = None
    prompt: str | None = None
    tool: ToolCall | None = None
    agent: AgentRef | None = None
    assistant_text: str | None = None
    transcript_ref: str | None = None

    EVENTS = EVENTS

    def __post_init__(self) -> None:
        if self.event not in EVENTS:
            raise ValueError(f"unknown hook event {self.event!r}; expected one of {EVENTS}")
        _check_schema_version(self.schema_version)

    @classmethod
    def from_json(cls, d: dict) -> "HookEvent":
        """Build an event from an adapter's JSON dict.

        Unknown keys are ignored so a client can add payload fields without
        breaking the model; a missing required field, an unknown event or a
        future schema version is an error the adapter must surface.
        """
        if not isinstance(d, dict):
            raise ValueError(f"hook payload must be an object, got {type(d).__name__}")
        return cls(
            harness=_required_text(d, "harness"),
            event=_required_text(d, "event"),
            session_id=_required_text(d, "session_id"),
            cwd=_required_text(d, "cwd"),
            schema_version=d.get("schema_version", SCHEMA_VERSION),
            turn_id=d.get("turn_id"),
            prompt=d.get("prompt"),
            tool=_tool_call(d.get("tool")),
            agent=_agent_ref(d.get("agent")),
            assistant_text=d.get("assistant_text"),
            transcript_ref=d.get("transcript_ref"),
        )


def _check_schema_version(version: object) -> None:
    # bool is an int subclass; a JSON `true` is a malformed version, not 1.
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version {version!r}; expected {SCHEMA_VERSION}")


def _required_text(d: dict, key: str) -> str:
    value = d.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"hook payload is missing a non-empty {key!r}")
    return value


def _tool_call(value: object) -> ToolCall | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"tool must be an object, got {type(value).__name__}")

    raw_name = _required_text(value, "raw_name")
    name = value.get("name")
    server = value.get("server")
    if name is None:
        server, name = canonical_tool(raw_name)
    elif not isinstance(name, str) or not name:
        raise ValueError(f"tool name must be a non-empty string, got {name!r}")
    if server is not None and not isinstance(server, str):
        raise ValueError(f"tool server must be a string or null, got {server!r}")

    tool_input = value.get("input")
    if tool_input is None:
        tool_input = {}
    if not isinstance(tool_input, dict):
        raise ValueError(f"tool input must be an object, got {type(tool_input).__name__}")

    return ToolCall(
        name=name,
        server=server,
        raw_name=raw_name,
        call_id=value.get("call_id"),
        input=tool_input,
        output=normalise_output(value.get("output")),
        is_error=bool(value.get("is_error", False)),
    )


def _agent_ref(value: object) -> AgentRef | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"agent must be an object, got {type(value).__name__}")
    return AgentRef(id=_required_text(value, "id"), type=value.get("type"))


@dataclass
class Block:
    """A refusal the adapter turns into the client's deny mechanism."""

    reason: str


@dataclass
class HookResponse:
    """What a hook wants the client to show or do, client-independent."""

    context: str | None = None
    notice: str | None = None
    tool_note: str | None = None
    block: Block | None = None

    def merge(self, other: "HookResponse") -> "HookResponse":
        """Return the union of two responses; the first block wins.

        Text fields join with a blank line so two hooks' context stays
        readable. Neither operand is modified.
        """
        return HookResponse(
            context=_join_text(self.context, other.context),
            notice=_join_text(self.notice, other.notice),
            tool_note=_join_text(self.tool_note, other.tool_note),
            block=self.block if self.block is not None else other.block,
        )


def _join_text(first: str | None, second: str | None) -> str | None:
    if not first:
        return second
    if not second:
        return first
    return f"{first}\n\n{second}"
