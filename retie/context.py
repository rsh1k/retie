"""Keeping large tool results out of the conversation.

A coding agent reads files, and files are long. Put every read straight into the
transcript and the context fills with material the model has already used and
will not look at again — but which is re-sent, and re-billed, on every
subsequent turn of the session. On a metered plan that is the single largest
avoidable cost, and it is what ends a long session early.

Manus's phrasing for the fix is the clearest: treat storage as the context, and
keep only a reference in the conversation. Their version writes to the file
system the agent operates on. That is wrong here — this agent's file system is
the user's repository, and scattering scratch files through someone's project is
a poor trade for tokens. So the payload is held beside the session and reached
through a `recall` tool instead.

The property that makes this safe is that nothing is discarded. The stub names
what was elided and how to get it back, so a model that needs the detail can
still have it. Compaction that loses information silently is how an agent starts
answering confidently from a gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Both numbers were measured, not guessed, on a task that reads two ~25 KB
# source files and summarises them:
#
#   compaction off          18,862 prompt tokens   19.25 units   6 calls
#   preview  420 / limit 1200   12,527             13.18        11 calls
#   preview 1500 / limit 2500    6,646              7.09         5 calls
#   preview 3000 / limit 4000   11,776             12.34         7 calls
#
# The first attempt used a 420-char preview and cost five extra round trips:
# too little to answer with, so the model called `recall` for nearly everything
# and paid the stub on top of the payload. A preview large enough to usually
# suffice is what turns this from a 32% saving into a 65% one at no extra calls.
#
# Single runs on one task, so treat the exact figures as indicative — but the
# direction and rough size held across every configuration tried.
INLINE_LIMIT = 2_500
PREVIEW = 1_500


@dataclass
class ContextStore:
    """Holds elided tool output for the life of the session."""

    items: dict[str, str] = field(default_factory=dict)
    _n: int = 0
    saved_chars: int = 0

    def stash(self, tool: str, text: str) -> str:
        """Store `text`, returning a stub to put in the transcript instead."""
        if len(text) <= INLINE_LIMIT:
            return text

        self._n += 1
        ref = f"r{self._n}"
        self.items[ref] = text
        # Counted once. The real saving is larger, because an inlined result is
        # re-sent on every later turn of the session, not just this one.
        self.saved_chars += len(text) - PREVIEW

        head = text[:PREVIEW]
        lines = text.count("\n") + 1
        return (
            f"{head}\n"
            f"…[truncated: {len(text):,} chars, {lines:,} lines from `{tool}`. "
            f"This is the first {PREVIEW} characters. "
            f"Call recall(ref=\"{ref}\") for the whole thing.]"
        )

    def recall(self, ref: str) -> str:
        text = self.items.get(ref)
        if text is None:
            known = ", ".join(sorted(self.items)) or "none"
            return f"[error] no such reference {ref!r}. Available: {known}"
        return text

    def summary(self) -> str:
        return (f"{len(self.items)} stashed result(s), "
                f"~{self.saved_chars:,} chars kept out of the transcript")
