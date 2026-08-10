#!/usr/bin/env python3
"""What the safety plane does, without spending a model call.

Runs the same four tool calls a real session would make, through the same
path, and shows the decision for each. No API key needed — this exercises the
plane, not the model, which is the part whose behaviour should be deterministic.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from retie.safety import SafetyPlane
from retie.tools import Workspace, edit_file, read_file, run_bash, write_file

BOLD, DIM, GREEN, RED, YELLOW, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="retie-demo-"))
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    ledger = root / "ledger.db"

    ws = Workspace.create(root)
    asked: list[str] = []

    def approve(tool: str, description: str, args: dict) -> bool:
        asked.append(description)
        print(f"  {YELLOW}→ asked the human:{RESET} {description}")
        return True  # the demo says yes so you can see what follows

    plane = SafetyPlane(ws, ledger_path=ledger, on_approval=approve)

    print(f"\n{BOLD}0. what this machine can enforce{RESET}\n")
    print(f"  sandbox   : {plane.sandbox_report}")
    print(f"  workspace : {plane.snapshot_note}")
    print(f"  {DIM}shell commands are {'confined and undoable' if plane.sandboxed else 'unconfined'}"
          f" → classified {'reversible' if plane.sandboxed else 'irreversible'}{RESET}")

    print(f"\n{BOLD}1. every tool call, and what the plane decided{RESET}\n")
    print(f"  {'tool':<7} {'allowed':<8} {'reversibility':<14} outcome")
    print(f"  {'-'*7} {'-'*8} {'-'*14} {'-'*34}")

    calls = [
        ("read", {"path": "calc.py"}, lambda: read_file(ws, "calc.py")),
        ("edit", {"path": "calc.py", "old": "a - b", "new": "a + b"},
         lambda: edit_file(ws, "calc.py", "a - b", "a + b")),
        ("write", {"path": "notes.md", "content": "scratch\n"},
         lambda: write_file(ws, "notes.md", "scratch\n")),
        ("bash", {"command": "echo shell-ran > out.txt && cat out.txt"},
         lambda: run_bash(ws, "echo shell-ran > out.txt && cat out.txt",
                          sandbox=plane.sandbox if plane.sandboxed else None)),
        ("read", {"path": "../../../etc/passwd"},
         lambda: read_file(ws, "../../../etc/passwd")),
    ]
    outcomes = {}
    for tool, args, fn in calls:
        o = plane.guard(tool, args, fn)
        outcomes.setdefault(tool, o)
        mark = f"{GREEN}yes{RESET}" if o.allowed else f"{RED}no {RESET}"
        print(f"  {tool:<7} {mark}      {o.reversibility:<14} {o.text.splitlines()[0][:34]}")

    print(f"\n  {DIM}every allowed call has an undo planned before it ran.")
    if plane.sandboxed:
        print(f"  bash needed no prompt here: confined + snapshotted = reversible.")
    else:
        print(f"  bash stopped for a human: unconfined, so nothing can undo it.")
    print(f"  the path escape was refused before the gate ever saw it.{RESET}")

    print(f"\n{BOLD}2. undo{RESET}\n")
    print(f"  calc.py now      : {(root / 'calc.py').read_text().strip().splitlines()[-1].strip()}")
    receipt = plane.undo(outcomes["edit"].verdict.action_id)
    print(f"  undo the edit    : ok={receipt.ok}")
    print(f"  calc.py restored : {(root / 'calc.py').read_text().strip().splitlines()[-1].strip()}")

    report = plane.undo_all()
    d = report.to_dict() if hasattr(report, "to_dict") else {}
    print(f"\n  undo the session : attempted={d.get('attempted')} "
          f"reversed={d.get('reversed_ok')} failed={d.get('failed')}")
    print(f"  notes.md         : {'removed' if not (root / 'notes.md').exists() else 'STILL THERE'}")

    print(f"\n{BOLD}3. the record, verified from a separate process{RESET}\n")
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys;from revoco.store import SqliteStore;"
         "s=SqliteStore(sys.argv[1]);e=s.load_ledger();"
         "print(f'  entries          : {len(e)}');"
         "prev=None;ok=True\n"
         "for x in e:\n"
         "    if prev is not None and getattr(x,'prev_hash',None)!=prev: ok=False;break\n"
         "    prev=getattr(x,'entry_hash',None)\n"
         "print(f'  chain            : {\"verifies\" if ok else \"BROKEN\"}')",
         str(ledger)],
        capture_output=True, text=True)
    print(proc.stdout.rstrip() or proc.stderr.rstrip())
    print(f"  {DIM}a log you can only check from inside the writing process is not evidence.{RESET}")

    shutil.rmtree(root, ignore_errors=True)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
