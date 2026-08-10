#!/usr/bin/env python3
"""Does the failover behave honestly when the provider says no?

The temptation with a model ladder is to treat every refusal the same and
rotate until something works. That is wrong here, and the test suite is built
around why: Ollama's limits are *per account*, so against a weekly cap every
model on the ladder is refused for the same reason. Rotating turns one clear
message into eighteen failed requests and a confused user.

So the assertions below are as much about what the router refuses to do as
about what it does.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tether.providers import (OpenAICompatBackend, ProviderRefused,
                              QuotaExhausted)
from tether.routing import (LimitKind, ModelInfo, Router, Usage, classify_limit,
                            tier_for)
from tether.toolspec import build_specs
from tether.tools import Workspace

BOLD, DIM, GREEN, RED, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[0m"
FAILS = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILS
    print(f"  {GREEN + 'pass' + RESET if ok else RED + 'FAIL' + RESET}  {label}")
    if detail:
        print(f"        {DIM}{detail}{RESET}")
    if not ok:
        FAILS += 1


# --------------------------------------------------------------------------
# A scriptable stand-in for Ollama Cloud.
# --------------------------------------------------------------------------
class FakeCloud:
    """Returns whatever each model is scripted to return."""

    def __init__(self, script: dict[str, tuple[int, str]]) -> None:
        self.script = script
        self.seen: list[str] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D102
                pass

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                model = body["model"]
                outer.seen.append(model)
                status, text = outer.script.get(model, (200, "ok"))
                if status != 200:
                    payload = json.dumps({"error": {"message": text}}).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in ({"choices": [{"delta": {"content": f"[{model}] "}}]},
                              {"choices": [{"delta": {}}],
                               "usage": {"prompt_tokens": 100, "completion_tokens": 50}}):
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def stop(self) -> None:
        self.srv.shutdown()


LADDER = [
    ModelInfo("gpt-oss:20b", 14_000_000_000, 1),
    ModelInfo("gpt-oss:120b", 65_000_000_000, 2),
    ModelInfo("qwen3.5:397b", 397_000_000_000, 3),
    ModelInfo("deepseek-v4-pro", 1_600_000_000_000, 4),
]


_CASE = [0]


def make(script, tmp, **kw):
    """Each case gets its own counter file.

    Sharing one would let a block written by an earlier case leak into a later
    one — which is the persistence feature working correctly, but it makes the
    tests read as failures and hides what each case is actually asserting.
    """
    _CASE[0] += 1
    cloud = FakeCloud(script)
    usage = Usage.load(Path(tmp) / f"usage-{_CASE[0]}.json")
    router = Router(list(LADDER), usage, **kw)
    switches: list[tuple[str, str, str]] = []
    be = OpenAICompatBackend(base_url=cloud.url(), api_key="test", router=router,
                             on_switch=lambda a, b, k: switches.append((a, b, k)))
    return cloud, router, be, switches


def run(be, ws):
    out: list[str] = []
    be.turn("sys", [{"role": "user", "content": "hi"}], build_specs(ws), out.append)
    return "".join(out)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="tether-route-")
    ws = Workspace.create(tempfile.mkdtemp())

    print(f"\n{BOLD}tiering is derived from size, anchored on the documented models{RESET}\n")
    check("gpt-oss:20b (14 GB) lands in tier 1", tier_for(14_000_000_000) == 1,
          "Ollama documents this model as usage level 1")
    check("deepseek-v4-pro (1.6 TB) lands in tier 4", tier_for(1_600_000_000_000) == 4,
          "Ollama documents this model as usage level 4")
    check("a model with no published size is treated as heavy", tier_for(0) == 4,
          "guessing cheap would spend the allowance faster than the user expects")

    print(f"\n{BOLD}refusals are classified, not lumped together{RESET}\n")
    check("weekly wording detected",
          classify_limit(429, "you have reached your weekly usage limit") == LimitKind.WEEKLY)
    check("session wording detected",
          classify_limit(429, "session limit reached, resets in 5 hours") == LimitKind.SESSION)
    check("Retry-After means short-term throttling",
          classify_limit(429, "slow down", {"retry-after": "30"}) == LimitKind.RATE)
    check("unrecognised 429 is UNKNOWN, not assumed fatal",
          classify_limit(429, "nope") == LimitKind.UNKNOWN,
          "assuming the worst would stop a user who could still work")

    print(f"\n{BOLD}cheapest model first{RESET}\n")
    cloud, router, be, _ = make({}, tmp)
    text = run(be, ws)
    check("started on the tier-1 model", cloud.seen[0] == "gpt-oss:20b",
          f"called: {cloud.seen}")
    check("usage was recorded against the model that served it",
          router.usage.calls == 1 and router.usage.weighted > 0,
          router.usage.summary())
    cloud.stop()

    print(f"\n{BOLD}a session limit escalates; a weekly cap does not{RESET}\n")
    cloud, router, be, switches = make(
        {"gpt-oss:20b": (429, "session limit reached, resets in 5 hours")}, tmp)
    text = run(be, ws)
    check("switched to the next model on a session limit", len(switches) == 1,
          f"{switches}")
    check("the turn still completed", "gpt-oss:120b" in text, text.strip())
    cloud.stop()

    cloud, router, be, switches = make(
        {m.name: (429, "you have reached your weekly usage limit")
         for m in LADDER}, tmp)
    try:
        run(be, ws)
        check("weekly cap raised QuotaExhausted", False, "no exception raised")
    except QuotaExhausted as exc:
        check("weekly cap raised QuotaExhausted immediately", exc.kind == LimitKind.WEEKLY)
        check("it did NOT rotate through the whole ladder", len(cloud.seen) == 1,
              f"tried {len(cloud.seen)} model(s): {cloud.seen} — "
              f"rotating would mean 4 pointless requests")
        check("no misleading 'switching to…' was printed", switches == [], f"{switches}")
    cloud.stop()

    print(f"\n{BOLD}exhausting the ladder is reported, not retried forever{RESET}\n")
    cloud, router, be, switches = make(
        {m.name: (429, "session limit reached") for m in LADDER}, tmp)
    try:
        run(be, ws)
        check("ladder exhaustion raised", False)
    except QuotaExhausted as exc:
        check("every model was tried before giving up", len(exc.tried) == 4,
              f"tried: {exc.tried}")
        check("all four were actually called", len(cloud.seen) == 4, f"{cloud.seen}")
    cloud.stop()

    print(f"\n{BOLD}--max-tier caps spend{RESET}\n")
    cloud, router, be, _ = make({}, tmp, max_tier=2)
    check("heavy models are excluded from the ladder",
          all(m.tier <= 2 for m in router.ladder),
          f"ladder: {[m.name for m in router.ladder]}")
    cloud.stop()

    print(f"\n{BOLD}a blocked model is remembered across runs{RESET}\n")
    usage = Usage.load(Path(tmp) / "persist.json")
    usage.block("gpt-oss:20b", 3600, "session")
    reloaded = Usage.load(Path(tmp) / "persist.json")
    check("the block survived a reload", not reloaded.available("gpt-oss:20b"),
          "so a fresh session does not re-hit a model that just refused")
    r2 = Router(list(LADDER), reloaded)
    check("the ladder skips it", r2.first().name == "gpt-oss:120b",
          f"first now: {r2.first().name}")

    print(f"\n{'all checks passed' if not FAILS else str(FAILS) + ' FAILED'}\n")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
