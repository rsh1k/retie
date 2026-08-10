"""Choosing a model, and what to do when the provider says no.

Read this first, because it contradicts the obvious design.

**Ollama Cloud's limits are per account, not per model.** Ollama's pricing page
says limits are "per plan", with session limits that reset every 5 hours and
weekly limits that reset every 7 days. So the intuitive strategy — rotate to a
different model when one is rate-limited — does not defeat a quota. When the
account's weekly allowance is gone, every model is gone with it, and a tool
that cycles through eighteen of them just turns one clear failure into eighteen
confusing ones.

What *does* extend how long the allowance lasts is the other half of the same
documentation: **usage is weighted by how heavy the model is.** Levels run from
1 for light models like `gpt-oss:20b` up to 4 for extra-heavy ones like
`deepseek-v4-pro`. A level-1 model therefore consumes roughly a quarter of what
a level-4 model does for the same work. Spending the cheapest model that can
actually do the task is what buys working hours; rotation is not.

So this module does three things:

1. **Derives tiers from live metadata** rather than a hard-coded table. The
   model list publishes on-disk size, which is a good proxy for how heavy a
   model is to run, and it is anchored on the two models Ollama's own docs name
   as level 1 and level 4. A table would be wrong the first time they ship a
   model; a derivation is only wrong if the relationship changes.
2. **Starts cheap and escalates on evidence.** The first attempt at a turn uses
   a light model. Escalation happens when that model actually fails, not
   because a task was guessed to be hard in advance.
3. **Tells session limits and weekly limits apart**, because one is worth
   waiting out and the other is not. Spinning through models against a weekly
   cap wastes the user's time and hides the real answer, which is that the
   quota is gone until a stated date.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The two anchors Ollama's pricing page names explicitly. Everything else is
# placed relative to these by size.
ANCHOR_LIGHT = "gpt-oss:20b"
ANCHOR_HEAVY = "deepseek-v4-pro"

# Size thresholds in bytes, derived from the anchors: gpt-oss:20b is ~14 GB and
# is documented as level 1; deepseek-v4-pro is ~1.6 TB and is documented as
# level 4. The two middle boundaries are interpolated on a log scale, which
# matches how the sizes actually cluster.
TIER_BOUNDS = (40e9, 200e9, 700e9)


@dataclass(frozen=True)
class ModelInfo:
    name: str
    size_bytes: int
    tier: int          # 1 (light) … 4 (extra heavy)

    @property
    def weight(self) -> int:
        """Roughly how much quota a call costs, relative to a light model."""
        return self.tier


def tier_for(size_bytes: int) -> int:
    """Place a model on the 1–4 scale by size.

    A model with no published size is treated as heavy. That is the safe
    direction: guessing "cheap" would spend the account's allowance faster than
    the user expects, and the whole point of tiering is to avoid that.
    """
    if size_bytes <= 0:
        return 4
    lo, mid, hi = TIER_BOUNDS
    if size_bytes < lo:
        return 1
    if size_bytes < mid:
        return 2
    if size_bytes < hi:
        return 3
    return 4


class LimitKind:
    """Why the provider refused, which decides whether waiting helps."""

    SESSION = "session"      # resets in ~5h — worth downshifting or waiting
    WEEKLY = "weekly"        # resets in ~7 days — nothing to do but stop
    RATE = "rate"            # short-term throttle — retry after a pause
    UNKNOWN = "unknown"


_WEEKLY = re.compile(r"week|weekly", re.I)
_SESSION = re.compile(r"session|5\s*hour|five\s*hour", re.I)


def classify_limit(status: int, body: str, headers: dict[str, str] | None = None) -> str:
    """Work out what kind of refusal this is from the response.

    Ollama does not document the exact bodies, so this matches on wording and
    degrades to UNKNOWN rather than pretending certainty. UNKNOWN is treated
    like a session limit — worth one downshift — because assuming the
    pessimistic case would stop a user who could still work.
    """
    if status != 429:
        return LimitKind.UNKNOWN
    text = body or ""
    if _WEEKLY.search(text):
        return LimitKind.WEEKLY
    if _SESSION.search(text):
        return LimitKind.SESSION
    if headers and headers.get("retry-after"):
        return LimitKind.RATE
    return LimitKind.UNKNOWN


@dataclass
class Usage:
    """Local accounting, because the provider does not expose a live balance.

    Ollama publishes neither the free tier's exact allowance nor a usage
    endpoint, so this cannot show "62% remaining" and does not pretend to. What
    it can do is record what this tool spent, weighted the way the provider
    weights it, so the burn rate is visible and a downshift is an informed
    decision rather than a guess.
    """

    path: Path
    calls: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    weighted: float = 0.0
    started: float = field(default_factory=time.time)
    blocked_until: dict[str, float] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Usage":
        if path.is_file():
            try:
                d = json.loads(path.read_text())
                return cls(path=path, calls=d.get("calls", 0),
                           prompt_tokens=d.get("prompt_tokens", 0),
                           output_tokens=d.get("output_tokens", 0),
                           weighted=d.get("weighted", 0.0),
                           started=d.get("started", time.time()),
                           blocked_until=d.get("blocked_until", {}))
            except (json.JSONDecodeError, OSError):
                pass          # a corrupt counter must not stop the session
        return cls(path=path)

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({
                "calls": self.calls, "prompt_tokens": self.prompt_tokens,
                "output_tokens": self.output_tokens, "weighted": self.weighted,
                "started": self.started, "blocked_until": self.blocked_until}))
        except OSError:
            pass

    def record(self, model: ModelInfo, prompt: int, output: int) -> None:
        self.calls += 1
        self.prompt_tokens += prompt
        self.output_tokens += output
        self.weighted += (prompt + output) / 1000.0 * model.weight
        self.save()

    def block(self, model: str, seconds: float, reason: str) -> None:
        self.blocked_until[model] = time.time() + seconds
        self.save()

    def available(self, model: str) -> bool:
        until = self.blocked_until.get(model, 0)
        if until and time.time() >= until:
            self.blocked_until.pop(model, None)
            self.save()
            return True
        return not until

    def summary(self) -> str:
        hours = max((time.time() - self.started) / 3600, 0.01)
        return (f"{self.calls} calls · {self.prompt_tokens + self.output_tokens:,} tokens "
                f"· {self.weighted:.1f} weighted units · {self.weighted / hours:.1f}/h")


class Router:
    """Picks a model, and reacts when the provider refuses.

    The ladder is ordered cheapest-first among models the caller is willing to
    use. A turn starts at the bottom and climbs only when the current rung
    actually fails — escalating on a guess about task difficulty would spend
    the expensive tiers on work the cheap ones could have done.
    """

    def __init__(self, models: list[ModelInfo], usage: Usage, *,
                 preferred: str | None = None, max_tier: int = 4) -> None:
        self.all = {m.name: m for m in models}
        self.usage = usage
        self.max_tier = max_tier
        pool = [m for m in models if m.tier <= max_tier]
        # Cheapest first; ties broken by size so the order is stable run to run.
        self.ladder = sorted(pool, key=lambda m: (m.tier, m.size_bytes, m.name))
        if preferred and preferred in self.all:
            chosen = self.all[preferred]
            self.ladder = [chosen] + [m for m in self.ladder if m.name != preferred]

    def candidates(self) -> list[ModelInfo]:
        """Models that are not currently blocked, cheapest first."""
        return [m for m in self.ladder if self.usage.available(m.name)]

    def first(self) -> ModelInfo | None:
        c = self.candidates()
        return c[0] if c else None

    def next_after(self, current: str, *, kind: str) -> ModelInfo | None:
        """What to try after `current` failed, given why it failed.

        On a weekly cap this returns nothing at all. The allowance is
        account-wide, so the next model is refused for the same reason — and
        answering "try this one instead" would be a lie that costs the user
        eighteen more failed requests before they learn the truth.
        """
        if kind == LimitKind.WEEKLY:
            return None
        for m in self.candidates():
            if m.name != current:
                return m
        return None

    def describe_ladder(self) -> list[str]:
        out = []
        for m in self.ladder:
            state = "" if self.usage.available(m.name) else "  (blocked)"
            size = f"{m.size_bytes / 1e9:.0f} GB" if m.size_bytes else "size unknown"
            out.append(f"tier {m.tier}  {m.name:<26} {size}{state}")
        return out


def discover(base_url: str = "https://ollama.com", timeout: float = 20.0) -> list[ModelInfo]:
    """Ask the provider what it runs, and tier it.

    Fetched live rather than hard-coded so a newly published model is usable
    the day it appears instead of the day this file is next edited.
    """
    import httpx

    r = httpx.get(f"{base_url}/api/tags", timeout=timeout)
    r.raise_for_status()
    out: list[ModelInfo] = []
    for m in r.json().get("models", []):
        name = m.get("name") or m.get("model")
        if not name:
            continue
        size = int(m.get("size") or 0)
        out.append(ModelInfo(name=name, size_bytes=size, tier=tier_for(size)))
    return out
