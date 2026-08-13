"""Terminal entry point.

Three commands, because three is what the tool actually does: hold a session,
put things back, and prove the record wasn't touched.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from .safety import SafetyPlane
from .tools import Workspace

DIM, BOLD, RED, GREEN, YELLOW, RESET = (
    "\033[2m", "\033[1m", "\033[31m", "\033[32m", "\033[33m", "\033[0m")


def _ask(tool: str, description: str, args: dict[str, Any]) -> bool:
    """Approval prompt. Shows what will happen, not the raw tool call."""
    print(f"\n{YELLOW}{BOLD}approval needed{RESET} {description}")
    if tool == "bash":
        print(f"{DIM}  this cannot be undone once it runs{RESET}")
    try:
        answer = input("  allow? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in {"y", "yes"}


def cmd_run(args: argparse.Namespace) -> int:
    from .agent import Agent  # imported late so `verify` works without a key
    from .providers import make_backend

    ws = Workspace.create(args.directory)
    plane = SafetyPlane(ws, ledger_path=Path(args.ledger),
                        on_approval=_ask, auto_approve=args.yes,
                        allow_network=args.network)
    router = None
    if args.provider == "ollama":
        from .routing import Router, Usage, discover
        try:
            models = discover()
        except Exception as exc:  # noqa: BLE001
            print(f"{RED}cannot reach Ollama Cloud:{RESET} {exc}")
            return 2
        usage = Usage.load(Path(args.usage).expanduser())
        router = Router(models, usage, preferred=args.model, max_tier=args.max_tier)

    def announce(old: str, new: str, kind: str) -> None:
        print(f"\n{YELLOW}  {old} refused ({kind}) → switching to {new}{RESET}")

    try:
        backend = make_backend(args.provider, args.model, effort=args.effort,
                               router=router, on_switch=announce)
    except Exception as exc:  # noqa: BLE001 - a missing key is a user problem
        print(f"{RED}cannot start:{RESET} {exc}")
        return 2
    agent = Agent(ws, plane, backend)

    print(f"{BOLD}retie{RESET} {DIM}· {backend.describe()}{RESET}")
    print(f"{DIM}  {ws.root} · ledger {args.ledger}{RESET}")
    if router is not None:
        pool = router.candidates()
        print(f"{DIM}  ladder: {len(pool)} models, cheapest first · "
              f"{router.usage.summary()}{RESET}")
    if plane.sandboxed:
        print(f"{DIM}  sandbox: {plane.sandbox_report} · workspace {plane.snapshot_note}")
        print(f"  shell commands are confined and undoable, so they run without asking{RESET}")
    else:
        why = (plane.sandbox_report.reason if not plane.sandbox_report.available
               else plane.snapshot_note)
        print(f"{YELLOW}  no sandbox: {why}{RESET}")
        print(f"{DIM}  shell commands cannot be undone here — each one will ask first{RESET}")
    if args.network:
        print(f"{YELLOW}  --network: the sandbox can reach the internet. Anything it "
              f"reads can leave.{RESET}")
    if args.yes:
        print(f"{YELLOW}  --yes: approvals are automatic. Shell commands run unprompted.{RESET}")
    print(f"{DIM}  ctrl-c to interrupt, ctrl-d to exit{RESET}\n")

    while True:
        try:
            line = input(f"{BOLD}› {RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in {"/exit", "/quit"}:
            break
        try:
            agent.send(line, lambda t: (sys.stdout.write(t), sys.stdout.flush()))
            print("\n")
        except KeyboardInterrupt:
            print(f"\n{DIM}interrupted{RESET}\n")
        except Exception as exc:  # noqa: BLE001 - keep the session alive
            from .providers import QuotaExhausted
            if isinstance(exc, QuotaExhausted):
                print(f"\n{RED}out of quota{RESET} ({exc.kind})")
                if exc.kind == "weekly":
                    print(f"{DIM}  This is an account-wide weekly cap, so no other")
                    print("  model will work either — it resets 7 days after it")
                    print(f"  started. Tried: {', '.join(exc.tried)}{RESET}\n")
                else:
                    print(f"{DIM}  Session limits reset about every 5 hours.")
                    print(f"  Tried: {', '.join(exc.tried)}{RESET}\n")
            else:
                print(f"\n{RED}error:{RESET} {type(exc).__name__}: {exc}\n")

    s = plane.stats()
    print(f"{DIM}session: {s}{RESET}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Re-read the ledger from disk and check the hash chain.

    This runs in a fresh process on purpose: an audit trail you can only verify
    from inside the process that wrote it is a log, not evidence.
    """
    # revoco's store, matching the one that wrote it. veritrail's has the
    # same class name and a different append_ledger signature.
    from revoco.store import SqliteStore

    store = SqliteStore(args.ledger)
    entries = store.load_ledger()
    ok = True
    prev = None
    for e in entries:
        if prev is not None and getattr(e, "prev_hash", None) != prev:
            ok = False
            break
        prev = getattr(e, "entry_hash", None)

    print(f"entries : {len(entries)}")
    print(f"chain   : {GREEN + 'verifies' + RESET if ok else RED + 'BROKEN' + RESET}")
    return 0 if ok else 1


def cmd_replay(args: argparse.Namespace) -> int:
    """Read a session back out of the ledger.

    The chain is verified first and the result is printed whichever way it
    goes. A replay whose integrity is unstated invites the reader to assume it,
    which is the assumption the ledger exists to remove.
    """
    from revoco.store import SqliteStore

    from .replay import read, render, to_markdown, verify

    try:
        entries = SqliteStore(args.ledger).load_ledger()
    except Exception as exc:  # noqa: BLE001
        print(f"{RED}cannot read {args.ledger}:{RESET} {exc}")
        return 1

    ok, note = verify(entries)
    views = read(entries)
    if not views:
        print(f"{DIM}no sessions in {args.ledger}{RESET}")
        return 0

    if args.session:
        views = [v for v in views if v.id.startswith(args.session)]
        if not views:
            print(f"{RED}no session starting with {args.session!r}{RESET}")
            return 1
    elif args.last:
        views = views[-1:]

    if args.markdown:
        for v in views:
            print(to_markdown(v, ok, note))
            print()
        return 0 if ok else 1

    banner = (f"{GREEN}chain verifies{RESET}" if ok else f"{RED}CHAIN BROKEN{RESET}")
    print(f"{DIM}{args.ledger} · {note} · {RESET}{banner}\n")
    for v in views:
        print(render(v))
        print()
    return 0 if ok else 1


def cmd_probe(args: argparse.Namespace) -> int:
    """Find out which models this account may actually call.

    Worth running once. Ollama publishes an eighteen-model catalogue, but a
    free plan is entitled to a fraction of it and the rest answer 403. Without
    this the ladder is mostly models the user cannot use, and every session
    pays a failed request to rediscover that.
    """
    from .providers import _key_from_config
    from .routing import Usage, discover, probe

    key = os.environ.get("OLLAMA_API_KEY") or _key_from_config()
    if not key:
        print(f"{RED}no API key{RESET} — see `retie run --help`")
        return 2
    usage = Usage.load(Path(args.usage).expanduser())
    models = sorted(discover(), key=lambda m: (m.tier, m.size_bytes))
    print(f"{BOLD}probing {len(models)} models{RESET} "
          f"{DIM}· one token each; entitlement is checked before generation{RESET}\n")

    def show(m, verdict, detail):
        mark = ({True: f"{GREEN}yes{RESET}", False: f"{RED}no {RESET}"}
                .get(verdict, f"{YELLOW}? {RESET}"))
        print(f"  {mark}  tier {m.tier}  {m.name:<28}{DIM}{detail}{RESET}")

    result = probe(models, usage, key, on_result=show)
    yes = sum(1 for v in result.values() if v is True)
    unknown = sum(1 for v in result.values() if v is None)
    print(f"\n  {yes}/{len(models)} confirmed accessible"
          + (f", {unknown} inconclusive (left eligible)" if unknown else "")
          + f" · cached in {args.usage}")
    usable = [m.name for m in models if result.get(m.name) is not False]
    if usable:
        print(f"  {DIM}the ladder will start on {usable[0]}{RESET}")
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    """Show what this tool has spent, and the ladder it would climb.

    Ollama publishes neither the free tier's allowance nor a usage endpoint, so
    this reports consumption, not remaining balance. Showing a percentage would
    mean inventing the denominator.
    """
    from .routing import Router, Usage, discover

    usage = Usage.load(Path(args.usage).expanduser())
    print(f"{BOLD}local usage counter{RESET} {DIM}· {args.usage}{RESET}")
    print(f"  {usage.summary()}")
    print(f"  {DIM}consumption only — Ollama does not publish the free-tier")
    print(f"  allowance or a balance endpoint, so remaining is unknowable{RESET}\n")
    try:
        router = Router(discover(), usage, max_tier=args.max_tier)
    except Exception as exc:  # noqa: BLE001
        print(f"{RED}could not reach Ollama Cloud:{RESET} {exc}")
        return 1
    print(f"{BOLD}ladder{RESET} {DIM}· cheapest first; a turn starts at the top{RESET}")
    for line in router.describe_ladder():
        print(f"  {line}")
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    """List what Ollama Cloud is serving, annotated for agent use.

    The live list is fetched rather than hard-coded, since it changes; the
    notes are ours and cover only the models we have an opinion about.
    """
    import httpx

    from .providers import OLLAMA_BASE_URL, OLLAMA_MODELS

    try:
        r = httpx.get(f"{OLLAMA_BASE_URL}/models", timeout=20)
        live = [m["id"] for m in r.json().get("data", [])]
    except Exception as exc:  # noqa: BLE001
        print(f"{RED}could not reach {OLLAMA_BASE_URL}:{RESET} {exc}")
        return 1

    print(f"{BOLD}Ollama Cloud{RESET} {DIM}· hosted, nothing downloaded · "
          f"key: https://ollama.com/settings/keys{RESET}\n")
    for m in live:
        note = OLLAMA_MODELS.get(m, "")
        star = f"{GREEN}*{RESET}" if m == "kimi-k2.7-code" else " "
        print(f" {star} {BOLD}{m}{RESET}")
        if note:
            for line in _wrap(note, 72):
                print(f"     {DIM}{line}{RESET}")
    print(f"\n {GREEN}*{RESET} default. Benchmark figures behind these notes are "
          f"vendor-run — treat as directional and test on your own work.")
    return 0


def _wrap(text: str, width: int) -> list[str]:
    import textwrap
    return textwrap.wrap(text, width)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="retie",
        description="A coding agent that plans the undo before it acts.")
    p.add_argument("--ledger", default="retie-ledger.db",
                   help="path to the tamper-evident action ledger")
    sub = p.add_subparsers(dest="cmd")

    run = sub.add_parser("run", help="start a session (default)")
    run.add_argument("directory", nargs="?", default=".",
                     help="project directory the agent may touch")
    run.add_argument("--provider", default="ollama", choices=["ollama", "anthropic"],
                     help="ollama runs hosted open models (no download, needs "
                          "OLLAMA_API_KEY); anthropic needs ANTHROPIC_API_KEY")
    run.add_argument("--model", default=None,
                     help="model id; see `retie models` for what Ollama Cloud runs")
    run.add_argument("--max-tier", type=int, default=4, choices=[1, 2, 3, 4],
                     help="heaviest model tier to use. Lower stretches the quota: "
                          "a tier-1 model costs about a quarter of a tier-4 one")
    run.add_argument("--usage", default="~/.retie/usage.json",
                     help="where to keep the local usage counter")
    run.add_argument("--effort", default="high",
                     choices=["low", "medium", "high", "xhigh", "max"],
                     help="how hard the model works per turn")
    run.add_argument("--network", action="store_true",
                     help="let sandboxed commands reach the network (needed for "
                          "installs; also means anything read can be sent out)")
    run.add_argument("--yes", action="store_true",
                     help="approve everything automatically (including shell commands)")
    run.set_defaults(func=cmd_run)

    ver = sub.add_parser("verify", help="re-read the ledger and check its hash chain")
    ver.set_defaults(func=cmd_verify)

    mod = sub.add_parser("models", help="list the hosted models Ollama Cloud runs")
    mod.set_defaults(func=cmd_models)

    rep = sub.add_parser("replay", help="read a recorded session back out of the ledger")
    rep.add_argument("--session", default="", help="session id, or a unique prefix")
    rep.add_argument("--last", action="store_true", help="only the most recent session")
    rep.add_argument("--markdown", action="store_true",
                     help="emit Markdown for sharing rather than a terminal view")
    rep.set_defaults(func=cmd_replay)

    prb = sub.add_parser("probe", help="find which models this account may call")
    prb.add_argument("--usage", default="~/.retie/usage.json")
    prb.set_defaults(func=cmd_probe)

    usg = sub.add_parser("usage", help="what this tool has spent, and the model ladder")
    usg.add_argument("--usage", default="~/.retie/usage.json")
    usg.add_argument("--max-tier", type=int, default=4, choices=[1, 2, 3, 4])
    usg.set_defaults(func=cmd_usage)

    args = p.parse_args(argv)
    if args.cmd is None:
        args = p.parse_args((argv or []) + ["run"])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
