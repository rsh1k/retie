"""Reading a session back out of the ledger.

Manus ships shareable replays: publish what the agent did as a link, for
handover or review. It is a genuinely good feature and the reason it works is
that they kept a full action history.

retie already keeps one, and keeps a stronger one — the chain is hash-linked, so
a replay can be checked rather than believed. That is the difference worth
having. A replay you cannot verify is a story about a session; a replay whose
chain verifies is evidence about it.

So this renders rather than records. Everything here is read back from entries
written at the time, which is why the renderer takes pains to distinguish what
was *authorised* from what was *attempted* and what *completed*, and to show
refusals as prominently as successes. An agent's own summary tends to omit what
it was stopped from doing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

BOLD, DIM, RED, GREEN, YELLOW, RESET = (
    "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[0m")


@dataclass
class Step:
    tool: str = ""
    description: str = ""
    allowed: bool = True
    effect: str = ""
    reason: str = ""
    reversibility: str = ""
    approved_by_human: bool = False
    at: float = 0.0
    undone: bool = False
    findings: list[str] = field(default_factory=list)


@dataclass
class SessionView:
    id: str = ""
    objective: str = ""
    model: str = ""
    backend: str = ""
    policy: str = ""
    sandbox: str = ""
    workspace: str = ""
    started: float = 0.0
    ended: float | None = None
    turns: int = 0
    steps: list[Step] = field(default_factory=list)

    @property
    def refused(self) -> list[Step]:
        return [s for s in self.steps if not s.allowed]

    @property
    def flagged(self) -> list[Step]:
        return [s for s in self.steps if s.findings]

    @property
    def irreversible(self) -> list[Step]:
        return [s for s in self.steps
                if s.allowed and s.reversibility not in ("reversible", "")]


def read(entries: list[Any]) -> list[SessionView]:
    """Group ledger entries into sessions, oldest first.

    The join is not obvious. A `verdict` carries the decision — allowed,
    reversibility, whether a person approved — but not the session or a readable
    description; the `action` entry carries those, keyed by the same action id.
    So actions are indexed first and verdicts are matched against them.

    A refused call never becomes an action, so it has no such record. Those are
    attributed to whichever session was open at that point in the chain, which
    is sound because the ledger is append-only and sessions do not interleave
    within one file. Dropping them instead would be the worst possible choice:
    a replay that shows only what succeeded is exactly the misleading artefact
    the ledger exists to prevent.
    """
    by_action: dict[str, dict[str, Any]] = {}
    for e in entries:
        p = e.payload if isinstance(e.payload, dict) else {}
        if e.kind == "action" and p.get("id"):
            by_action[p["id"]] = p

    sessions: dict[str, SessionView] = {}
    order: list[str] = []
    current = "(before session records existed)"
    pending_findings: list[str] = []

    def bucket(sid: str) -> SessionView:
        if sid not in sessions:
            sessions[sid] = SessionView(id=sid)
            order.append(sid)
        return sessions[sid]

    for e in entries:
        p = e.payload if isinstance(e.payload, dict) else {}

        if e.kind == "session.start":
            current = p.get("session_id", "?")
            v = bucket(current)
            v.objective = p.get("objective", "")
            v.model = p.get("model", "")
            v.backend = p.get("backend", "")
            v.policy = p.get("policy", "")
            v.sandbox = p.get("sandbox", "")
            v.workspace = p.get("workspace", "")
            v.started = p.get("started_at", 0.0)

        elif e.kind == "session.end":
            v = bucket(p.get("session_id", current))
            v.ended = p.get("ended_at")
            v.turns = p.get("turns", 0)

        elif e.kind == "inspection":
            # Written just before the verdict for the same call, so it is held
            # and attached when that verdict arrives. Keeping it as its own
            # entry rather than folding it into the verdict is deliberate: what
            # was noticed and what was decided are separate facts, and a replay
            # that shows only the decision cannot show a finding that was
            # overridden.
            pending_findings = [
                f"{f.get('severity')}: {f.get('rule')}"
                for f in p.get("findings", [])]

        elif e.kind == "verdict":
            act = by_action.get(p.get("action_id", ""), {})
            v = bucket(act.get("session_id") or current)
            v.steps.append(Step(
                tool=p.get("tool", ""),
                description=act.get("description") or p.get("reason", ""),
                allowed=bool(p.get("allowed")),
                effect=p.get("effect", ""),
                reason=p.get("reason", ""),
                reversibility=p.get("reversibility", ""),
                approved_by_human=bool(p.get("approved_by_human")),
                at=act.get("occurred_at") or getattr(e, "recorded_at", 0.0) or 0.0,
                findings=pending_findings,
            ))
            pending_findings = []

        elif e.kind in ("reversal_commit", "reversal"):
            aid = p.get("action_id")
            if aid:
                act = by_action.get(aid, {})
                v = bucket(act.get("session_id") or current)
                for st in v.steps:
                    if st.description == act.get("description"):
                        st.undone = True

    return [sessions[k] for k in order]


def verify(entries: list[Any]) -> tuple[bool, str]:
    """Walk the hash chain. This is what makes the replay evidence."""
    prev = None
    for i, e in enumerate(entries):
        this_prev = getattr(e, "prev_hash", None)
        if prev is not None and this_prev != prev:
            return False, f"chain breaks at entry {i}"
        prev = getattr(e, "entry_hash", None)
    return True, f"{len(entries)} entries, chain intact"


def _clock(t: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t)) if t else "--:--:--"


def render(view: SessionView, *, colour: bool = True) -> str:
    """A session as a person would want to read it."""
    c = (lambda x: x) if colour else (lambda x: "")
    B, D, R, G, Y, X = (c(BOLD), c(DIM), c(RED), c(GREEN), c(YELLOW), c(RESET))
    out: list[str] = []

    out.append(f"{B}{view.objective or '(no objective recorded)'}{X}")
    meta = [x for x in (view.model, view.policy, view.sandbox) if x]
    out.append(f"{D}{view.id} · {' · '.join(meta)}{X}")
    if view.workspace:
        out.append(f"{D}{view.workspace}{X}")
    out.append("")

    if not view.steps:
        out.append(f"{D}  no actions recorded{X}")
    for s in view.steps:
        if not s.allowed:
            # Only append the reason when it is not already standing in as the
            # description, or a refusal prints its own explanation twice.
            same = s.description.strip() == s.reason.strip()
            mark = f"{R}refused{X}"
            note = "" if same else f"  {D}{s.reason[:60]}{X}"
        elif s.reversibility != "reversible":
            mark, note = f"{Y}{s.reversibility}{X}", (
                f"  {D}approved by a person{X}" if s.approved_by_human else "")
        else:
            mark, note = f"{G}ok{X}", ""
        out.append(f"  {D}{_clock(s.at)}{X}  {mark:<18} {s.tool:<8} {s.description[:52]}{note}")
        for f in s.findings:
            colour = R if f.startswith("high") else Y
            out.append(f"              {colour}⚑ {f}{X}")

    out.append("")
    dur = f"{view.ended - view.started:.0f}s" if view.ended and view.started else "unfinished"
    out.append(f"{D}  {len(view.steps)} actions · {len(view.refused)} refused · "
               f"{len(view.irreversible)} not reversible · {len(view.flagged)} flagged · "
               f"{view.turns} turns · {dur}{X}")
    return "\n".join(out)


def to_markdown(view: SessionView, chain_ok: bool, chain_note: str) -> str:
    """A shareable version. Plain Markdown so it pastes anywhere."""
    lines = [f"# {view.objective or 'Session'}", ""]
    lines.append(f"- **Session** `{view.id}`")
    if view.model:
        lines.append(f"- **Model** `{view.model}` via {view.backend}")
    if view.policy:
        lines.append(f"- **Policy** `{view.policy}`")
    if view.sandbox:
        lines.append(f"- **Sandbox** {view.sandbox}")
    lines.append(f"- **Integrity** {'chain verifies' if chain_ok else 'CHAIN BROKEN'} "
                 f"({chain_note})")
    lines += ["", "| Time | Outcome | Tool | What | Flagged |", "|---|---|---|---|---|"]
    for s in view.steps:
        outcome = ("refused" if not s.allowed
                   else s.reversibility + (" · human approved" if s.approved_by_human else ""))
        # Escaped outside the f-string: a backslash inside one is only legal on
        # Python 3.12+, and this package supports 3.10.
        what = s.description[:70].replace("|", "\\|")
        flagged = "<br>".join(s.findings) or "—"
        lines.append(f"| {_clock(s.at)} | {outcome} | `{s.tool}` | {what} | {flagged} |")
    lines += ["",
              (f"{len(view.steps)} actions, {len(view.refused)} refused, "
               f"{len(view.irreversible)} not reversible, "
               f"{len(view.flagged)} flagged by inspection."), "",
              ("_Rendered from a hash-chained ledger written as the session ran, "
               "not from the agent's account of it._")]
    return "\n".join(lines)
