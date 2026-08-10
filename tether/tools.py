"""The tool surface, and what each tool costs to undo.

Every tool is declared alongside an inverse: the operation that puts the world
back. That pairing is the point of this file. A tool whose inverse is unknown
is not a tool the agent gets to use unsupervised — it is a request for a human.

The classification is deliberately conservative. `bash` runs arbitrary
commands, so nothing here can know what it did or how to undo it; it is marked
irreversible and routed to a human rather than pattern-matched against a list
of "safe" commands. That guess is exactly the failure other agent runtimes
make, and it is where their own documentation admits the policy layer is blind:
allowing `exec` while denying `write` does not make the shell read-only.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from revoco.reversal import InverseSpec, Reversibility

MAX_READ_BYTES = 400_000
MAX_OUTPUT_CHARS = 30_000


class ToolError(RuntimeError):
    """Raised for expected failures; surfaced to the model as a tool result."""


@dataclass
class Workspace:
    """The directory the agent is allowed to touch, and the undo snapshots.

    Path confinement is enforced here rather than in the policy layer because
    it is a property of the filesystem, not of the request: a symlink that
    escapes the root is still an escape however the call was authorised.
    """

    root: Path
    snapshots: Path
    _saved: dict[str, str] = field(default_factory=dict)

    @classmethod
    def create(cls, root: str | Path) -> "Workspace":
        root = Path(root).resolve()
        snaps = Path(tempfile.mkdtemp(prefix="tether-snap-"))
        return cls(root=root, snapshots=snaps)

    def resolve(self, path: str) -> Path:
        """Resolve `path` inside the workspace, refusing anything that escapes.

        Resolution happens before the containment check so that `..` segments
        and symlinked parents are collapsed first — checking the raw string
        would pass a path that only escapes once the OS follows it.
        """
        candidate = (self.root / path).resolve() if not os.path.isabs(path) else Path(path).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ToolError(f"path escapes the workspace root: {path}")
        return candidate

    def snapshot(self, path: Path) -> str | None:
        """Copy a file aside so a later write can be undone. Returns a token."""
        if not path.is_file():
            return None
        token = f"{len(self._saved)}-{path.name}"
        dest = self.snapshots / token
        shutil.copy2(path, dest)
        self._saved[token] = str(path)
        return token

    def restore(self, token: str) -> str:
        src = self.snapshots / token
        target = self._saved.get(token)
        if target is None or not src.is_file():
            raise ToolError(f"no snapshot for token {token}")
        shutil.copy2(src, target)
        return target


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n… [clipped, {len(text) - MAX_OUTPUT_CHARS} more chars]"


# --------------------------------------------------------------------------
# Tool implementations. Each takes the workspace explicitly so they stay
# testable without a live agent.
# --------------------------------------------------------------------------

def read_file(ws: Workspace, path: str) -> str:
    target = ws.resolve(path)
    if not target.is_file():
        raise ToolError(f"not a file: {path}")
    if target.stat().st_size > MAX_READ_BYTES:
        raise ToolError(f"file is {target.stat().st_size} bytes; too large to read whole")
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    return _clip("\n".join(f"{i:6d}\t{l}" for i, l in enumerate(lines, 1)))


def write_file(ws: Workspace, path: str, content: str) -> str:
    target = ws.resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.is_file()
    target.write_text(content, encoding="utf-8")
    return f"{'overwrote' if existed else 'created'} {target} ({len(content)} chars)"


def edit_file(ws: Workspace, path: str, old: str, new: str) -> str:
    target = ws.resolve(path)
    if not target.is_file():
        raise ToolError(f"not a file: {path}")
    body = target.read_text(encoding="utf-8")
    hits = body.count(old)
    if hits == 0:
        raise ToolError("old text not found; read the file and match it exactly")
    if hits > 1:
        raise ToolError(f"old text appears {hits} times; include more context to make it unique")
    target.write_text(body.replace(old, new, 1), encoding="utf-8")
    return f"edited {target}"


def glob_files(ws: Workspace, pattern: str) -> str:
    hits = [str(p.relative_to(ws.root)) for p in ws.root.rglob("*")
            if p.is_file() and fnmatch.fnmatch(str(p.relative_to(ws.root)), pattern)]
    return _clip("\n".join(sorted(hits)[:500]) or "(no matches)")


def grep_files(ws: Workspace, pattern: str, glob: str = "*") -> str:
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        raise ToolError(f"bad regex: {exc}") from exc
    out: list[str] = []
    for p in sorted(ws.root.rglob("*")):
        if not p.is_file() or not fnmatch.fnmatch(str(p.relative_to(ws.root)), glob):
            continue
        try:
            for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    out.append(f"{p.relative_to(ws.root)}:{n}: {line.strip()[:200]}")
                    if len(out) >= 300:
                        return _clip("\n".join(out))
        except OSError:
            continue
    return _clip("\n".join(out) or "(no matches)")


def run_bash(ws: Workspace, command: str, timeout: int = 120, sandbox: object = None) -> str:
    """Run a shell command, confined if a sandbox was supplied.

    The unconfined branch exists because a host without working user
    namespaces should still be usable — but on that host the command is
    classified irreversible and a human has already approved it by the time
    execution reaches here.
    """
    if sandbox is not None:
        proc = sandbox.run(command, timeout=timeout)
    else:
        proc = subprocess.run(command, shell=True, cwd=ws.root, capture_output=True,
                              text=True, timeout=timeout)
    parts = []
    if proc.stdout:
        parts.append(proc.stdout)
    if proc.stderr:
        parts.append(f"[stderr]\n{proc.stderr}")
    parts.append(f"[exit {proc.returncode}]")
    return _clip("\n".join(parts))


# --------------------------------------------------------------------------
# Reversibility declarations
# --------------------------------------------------------------------------

# `read`, `glob` and `grep` observe without changing anything, so their inverse
# is a no-op and they never need approval. `write` and `edit` are reversible
# because we snapshot the prior contents first — the undo plan exists before
# the action runs, which is the only moment the pre-action state is knowable.
# `bash` gets no inverse: an arbitrary shell command cannot be undone by
# anything this package knows how to write.
INVERSE_SPECS = [
    # A read has no effect, so its inverse is a no-op. Naming that explicitly
    # rather than leaving these unregistered matters: an unregistered tool
    # classifies as UNKNOWN, and UNKNOWN is treated as potentially irreversible
    # — which would put an approval prompt in front of every file read.
    InverseSpec(tool="read", kind=Reversibility.REVERSIBLE, inverse_tool="noop",
                notes="read-only; nothing to undo"),
    InverseSpec(tool="glob", kind=Reversibility.REVERSIBLE, inverse_tool="noop",
                notes="read-only; nothing to undo"),
    InverseSpec(tool="grep", kind=Reversibility.REVERSIBLE, inverse_tool="noop",
                notes="read-only; nothing to undo"),
    InverseSpec(tool="write", kind=Reversibility.REVERSIBLE,
                inverse_tool="restore_snapshot",
                # snapshot_fields says what to capture before acting; arg_map
                # says how those captured values reach the inverse. Without the
                # map the plan records a snapshot it never passes on, and the
                # undo fails at execution with a missing argument.
                snapshot_fields=("path", "prior_contents"),
                arg_map=(("path", "snapshot.path"),
                         ("prior_contents", "snapshot.prior_contents")),
                notes="prior file contents copied aside before the write"),
    InverseSpec(tool="edit", kind=Reversibility.REVERSIBLE,
                inverse_tool="restore_snapshot",
                # snapshot_fields says what to capture before acting; arg_map
                # says how those captured values reach the inverse. Without the
                # map the plan records a snapshot it never passes on, and the
                # undo fails at execution with a missing argument.
                snapshot_fields=("path", "prior_contents"),
                arg_map=(("path", "snapshot.path"),
                         ("prior_contents", "snapshot.prior_contents")),
                notes="prior file contents copied aside before the edit"),
    InverseSpec(tool="bash", kind=Reversibility.IRREVERSIBLE,
                residue="arbitrary shell effects outside the workspace",
                notes="a shell command's effects are not knowable in advance, "
                      "so this is never auto-approved"),
]


def inverse_specs(*, sandboxed: bool) -> list[InverseSpec]:
    """The reversibility declarations for this machine.

    `bash` is the only entry that changes, and it changes for a real reason
    rather than a configuration preference. Unconfined, a shell command can
    reach the whole filesystem and the network, and no inverse can be written
    for it. Confined to a directory we snapshot first, with no network, the
    inverse is exactly one operation: put the directory back.

    So reversibility is a property of the *deployment*, not of the tool name.
    On a host with no working sandbox this returns the irreversible spec and
    every shell command stops for a human — the safe direction, reached by
    checking rather than assuming.
    """
    specs = [s for s in INVERSE_SPECS if s.tool != "bash"]
    if not sandboxed:
        return specs + [s for s in INVERSE_SPECS if s.tool == "bash"]
    return specs + [
        InverseSpec(
            tool="bash", kind=Reversibility.REVERSIBLE,
            inverse_tool="restore_workspace",
            snapshot_fields=("workspace_token",),
            arg_map=(("token", "snapshot.workspace_token"),),
            residue="processes the command started, and anything it wrote "
                    "into directories excluded from the snapshot",
            notes="confined to the workspace with no network; the workspace "
                  "is copied before the command runs",
        )
    ]
