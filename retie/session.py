"""What the agent was asked, recorded alongside what it did.

The ledger was already good at the hard half of this. It distinguishes what the
agent was *authorised* to do from what it *attempted* and what it *accomplished*
— a verdict, then an action, then a commit — and it keeps refusals, so it records
what the agent was stopped from doing as well as what it managed. Published work
on evidentiary adequacy for agent oversight names those as the properties most
runtime logs lack.

What it could not answer was *why*. Read the old ledger and you can see that a
file was edited, by whom, under which delegation, and whether it could be undone
— but not what anyone asked for, and not which model decided. "Why did it delete
that?" was unanswerable from the record, which is the first question anybody
actually asks. The EU AI Act's Article 12 names inputs explicitly for the same
reason.

So a session record now opens the chain: the objective in the user's own words,
the model that served it, and digests of the instructions in force. Every action
afterwards carries the session id, so a single verdict can be traced back to the
sentence that caused it.

Prompts are recorded as digests rather than text by default. The objective is
the user's own sentence and is kept verbatim, because that is the bit an auditor
needs; the system prompt is ours, unchanging, and reproducible from the digest.
"""

from __future__ import annotations

import hashlib
import os
import platform
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


def digest(text: str) -> str:
    """Short, stable fingerprint of an instruction."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class Session:
    """One interactive session, and the facts that explain it."""

    id: str = field(default_factory=lambda: f"ses_{uuid.uuid4().hex[:20]}")
    started: float = field(default_factory=time.time)
    objective: str = ""
    model: str = ""
    backend: str = ""
    system_digest: str = ""
    workspace: str = ""
    sandbox: str = ""
    policy: str = ""
    turns: int = 0
    ended: float | None = None

    def opening(self) -> dict[str, Any]:
        """The payload written when the session starts."""
        return {
            "session_id": self.id,
            "started_at": self.started,
            # Verbatim: this is the instruction the actions have to be explained
            # against, and a digest of it would explain nothing.
            "objective": self.objective,
            "model": self.model,
            "backend": self.backend,
            "system_prompt_sha256_16": self.system_digest,
            "workspace": self.workspace,
            "sandbox": self.sandbox,
            "policy": self.policy,
            # Enough to reproduce the run; not enough to identify the machine.
            "host": {"python": platform.python_version(),
                     "platform": platform.system(),
                     "pid": os.getpid()},
        }

    def closing(self) -> dict[str, Any]:
        self.ended = time.time()
        return {
            "session_id": self.id,
            "ended_at": self.ended,
            "duration_seconds": round(self.ended - self.started, 1),
            "turns": self.turns,
        }


def record(plane: Any, kind: str, payload: dict[str, Any]) -> bool:
    """Write a non-action entry into the same hash chain.

    `_append` is private on revoco's control plane, and reaching past the
    underscore is a real smell. It is done here deliberately rather than
    carelessly: the public surface covers authorising and confirming *actions*,
    and a session record is not an action. The alternative — appending via
    `plane.ledger` — writes to the in-memory chain but skips the durable store,
    which would leave the session record present in one and absent in the other.
    Silently divergent ledgers are worse than a private call.

    revoco should expose this as a public `record()`; until it does, this
    degrades to a warning rather than losing the session.
    """
    try:
        plane.plane._append(kind, payload)
        return True
    except Exception:  # noqa: BLE001 - evidence is valuable, not load-bearing
        return False
