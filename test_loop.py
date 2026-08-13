#!/usr/bin/env python3
"""Does the loop still gate every tool call, now that a second provider exists?

The risk a provider abstraction introduces is a second path to the filesystem
that skips the plane. A fake backend lets that be tested without a key and
without a model: it emits exactly the tool calls a real one would, including
the malformed ones weaker models actually produce.

The assertions are about the gate, not about the model. Whether a given model
picks good tools is its problem; whether a bad pick can do damage is ours.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from retie.agent import Agent
from retie.providers import ToolCall, Turn
from retie.safety import SafetyPlane
from retie.tools import Workspace

BOLD, DIM, GREEN, RED, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[0m"


class FakeBackend:
    """Replays a scripted sequence of turns. Runs no tools, like a real backend."""

    name = "fake"

    def __init__(self, turns: list[Turn], shape: str = "anthropic") -> None:
        self.turns = list(turns)
        self.shape = shape
        self.seen: list[list[dict]] = []

    def describe(self) -> str:
        return f"fake/{self.shape}"

    def turn(self, system, messages, specs, on_text) -> Turn:
        self.seen.append(list(messages))
        t = self.turns.pop(0) if self.turns else Turn(text="done")
        if t.text:
            on_text(t.text)
        return t

    def assistant_message(self, turn):
        if self.shape == "anthropic":
            return {"role": "assistant", "content": turn.raw or turn.text}
        msg = {"role": "assistant", "content": turn.text or None}
        if turn.tool_calls:
            msg["tool_calls"] = [{"id": c.id, "type": "function",
                                  "function": {"name": c.name,
                                               "arguments": json.dumps(c.args)}}
                                 for c in turn.tool_calls]
        return msg

    def tool_results(self, results):
        if self.shape == "anthropic":
            return {"role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": c.id,
                                 "content": o} for c, o in results]}
        return [{"role": "tool", "tool_call_id": c.id, "content": o}
                for c, o in results]


def run_case(name: str, turns: list[Turn], shape: str, *, auto_approve=True):
    root = Path(tempfile.mkdtemp(prefix="retie-loop-"))
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    ws = Workspace.create(root)
    gated: list[str] = []

    plane = SafetyPlane(ws, auto_approve=auto_approve)
    original = plane.guard

    def spy(tool, args, execute):
        gated.append(tool)
        return original(tool, args, execute)

    plane.guard = spy  # type: ignore[method-assign]
    agent = Agent(ws, plane, FakeBackend(turns, shape))
    out: list[str] = []
    agent.send("do the thing", out.append)
    return root, gated, "".join(out), agent


def main() -> int:
    failures = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        mark = f"{GREEN}pass{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark}  {label}")
        if detail:
            print(f"        {DIM}{detail}{RESET}")
        if not ok:
            failures += 1

    print(f"\n{BOLD}every tool call passes the gate — both wire shapes{RESET}\n")
    for shape in ("anthropic", "openai"):
        turns = [
            Turn(text="reading. ", tool_calls=[ToolCall("t1", "read", {"path": "calc.py"})]),
            Turn(text="fixing. ", tool_calls=[
                ToolCall("t2", "edit", {"path": "calc.py", "old": "a - b", "new": "a + b"})]),
            Turn(text="done."),
        ]
        root, gated, text, _ = run_case(shape, turns, shape)
        body = (root / "calc.py").read_text()
        check(f"{shape}: both calls went through plane.guard",
              gated == ["read", "edit"], f"gated={gated}")
        check(f"{shape}: the edit actually applied",
              "a + b" in body, body.strip().replace("\n", " | "))
        check(f"{shape}: assistant text streamed out",
              "reading." in text and "done." in text)

    print(f"\n{BOLD}the failure modes weaker models actually produce{RESET}\n")

    # A hallucinated tool name must not reach the gate as an unknown tool, and
    # must not end the session.
    root, gated, text, _ = run_case(
        "unknown", [Turn(tool_calls=[ToolCall("t1", "delete_everything", {})]),
                    Turn(text="ok")], "openai")
    check("unknown tool is refused without touching the gate", gated == [],
          f"gated={gated}")
    check("session continued after the unknown tool", "ok" in text)

    # Malformed JSON arguments: the backend marks them, the loop must not
    # execute anything and must tell the model what went wrong.
    root, gated, text, agent = run_case(
        "badjson", [Turn(tool_calls=[ToolCall("t1", "write",
                                              {"__parse_error__": "{path:"})]),
                    Turn(text="ok")], "openai")
    check("malformed arguments never reach the tool", gated == [], f"gated={gated}")
    sent_back = json.dumps(agent.messages)
    check("model was told the arguments were bad", "valid JSON" in sent_back)

    # A refused approval must come back as a tool result, not an exception.
    root, gated, text, agent = run_case(
        "denied", [Turn(tool_calls=[ToolCall("t1", "bash", {"command": "rm -rf /"})]),
                   Turn(text="understood")], "openai", auto_approve=False)
    check("denied call still recorded as a gate decision", gated == ["bash"])
    check("session survived the denial", "understood" in text)

    # The loop must stop rather than spin forever.
    print(f"\n{BOLD}the loop terminates{RESET}\n")
    forever = [Turn(tool_calls=[ToolCall(f"t{i}", "read", {"path": "calc.py"})])
               for i in range(80)]
    root, gated, text, _ = run_case("runaway", forever, "openai")
    check("runaway loop stopped at the step cap", "stopped after" in text,
          f"{len(gated)} tool calls before stopping")

    # ---- large results are kept out of the transcript, reversibly ----------
    print(f"\n{BOLD}large tool results are elided but never lost{RESET}\n")
    from retie.context import INLINE_LIMIT, ContextStore

    store = ContextStore()
    small = "x" * (INLINE_LIMIT - 1)
    big = "line\n" * 4000

    check("a small result is left alone", store.stash("read", small) == small,
          f"{len(small)} chars, under the {INLINE_LIMIT} limit")

    stub = store.stash("read", big)
    check("a large result is replaced by a stub", len(stub) < len(big) / 5,
          f"{len(big):,} chars became {len(stub):,}")
    check("the stub says how to get the rest", "recall(ref=" in stub)
    check("nothing is lost", store.recall("r1") == big,
          "recall returns the payload byte for byte")
    check("a bad reference is reported, not raised",
          "no such reference" in store.recall("r99"))

    # The whole point is fewer tokens on later turns, so the saving has to be
    # real rather than a reshuffle.
    check("the saving is counted honestly",
          store.saved_chars == len(big) - 1500,
          store.summary())

    print(f"\n{'all checks passed' if not failures else str(failures) + ' FAILED'}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
