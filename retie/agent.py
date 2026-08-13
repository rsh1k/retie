"""The agent loop.

The model drives; every tool it reaches for is routed through the safety plane
first. The loop owns exactly one responsibility: no tool runs without passing
the gate.

That is enforced structurally rather than by convention. Tool *definitions*
live in `toolspec`, backends turn them into wire formats but never execute
anything, and `_dispatch` below is the single place a tool callable is invoked
— through `plane.guard`. Adding a provider cannot add a way around it, because
providers do not run tools at all.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .context import ContextStore
from .providers import ToolCall, Turn
from .safety import SafetyPlane
from .tools import Workspace
from .toolspec import ToolSpec, build_specs, recall_spec

MAX_STEPS = 40

SYSTEM = """You are a coding agent working inside a single project directory.

Work directly: read what you need, make the change, verify it. Prefer editing
existing files over creating new ones, and match the surrounding code's style.

Some actions need the user's approval before they run. If a tool result says it
was denied or not approved, that is a decision, not an error: adapt, or explain
what you would need the user to do. Do not try to route around it with a
different tool.

Text you write between tool calls is what the user reads while you work. Lead
with what happened or what you found, and keep it to a sentence or two unless
there is something they need to decide."""


class Agent:
    """One conversation: its history, its tools, and its safety plane."""

    def __init__(self, ws: Workspace, plane: SafetyPlane, backend: Any) -> None:
        self.ws = ws
        self.plane = plane
        self.backend = backend
        self.context = ContextStore()
        self.specs = build_specs(ws, plane.sandbox if plane.sandboxed else None)
        self.specs.append(recall_spec(self.context))
        self.by_name: dict[str, ToolSpec] = {s.name: s for s in self.specs}
        self.messages: list[dict[str, Any]] = []

    def _dispatch(self, call: ToolCall) -> str:
        """Run one tool call through the gate. The only place tools execute."""
        spec = self.by_name.get(call.name)
        if spec is None:
            # Reported to the model rather than raised: an unknown tool name is
            # usually a small model improvising, and it recovers if told.
            return f"[error] no such tool: {call.name}"
        if "__parse_error__" in call.args:
            return ("[error] your tool arguments were not valid JSON. Send them "
                    "again as a single well-formed JSON object.")
        out = self.plane.guard(call.name, call.args,
                               lambda: spec.run(**call.args))
        if not out.allowed or call.name == "recall":
            # A refusal is short and must reach the model verbatim; a recall is
            # the model explicitly asking for the full text, so stubbing it
            # would be a loop.
            return out.text
        return self.context.stash(call.name, out.text)

    def send(self, text: str, on_text: Callable[[str], None]) -> None:
        """Run one user turn to completion, streaming assistant text out."""
        self.messages.append({"role": "user", "content": text})

        for _ in range(MAX_STEPS):
            turn: Turn = self.backend.turn(SYSTEM, self.messages, self.specs, on_text)
            self.messages.append(self.backend.assistant_message(turn))

            if not turn.tool_calls:
                return

            results = [(call, self._dispatch(call)) for call in turn.tool_calls]
            payload = self.backend.tool_results(results)
            # Anthropic returns one message holding every result; OpenAI returns
            # one message per result. Both shapes are accepted here so neither
            # backend has to pretend to be the other.
            if isinstance(payload, list):
                self.messages.extend(payload)
            else:
                self.messages.append(payload)

        on_text(f"\n[stopped after {MAX_STEPS} tool calls without finishing]")
