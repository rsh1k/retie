"""Confining shell commands, and what that does to reversibility.

The interesting property here is not the sandbox itself — it is what the
sandbox changes about classification.

An unconfined shell command is irreversible: it can reach the whole filesystem
and the network, and nothing can state its inverse. Confine it to a directory
we can copy, cut its network, and the inverse becomes writable: restore the
directory. So the same tool is `IRREVERSIBLE` on a machine with no sandbox and
`REVERSIBLE` on one with a working sandbox and an affordable snapshot.

That matters for a reason beyond tidiness. A gate that prompts for every shell
command trains the user to approve reflexively, and a control that is always
approved is decoration. Making sandboxed commands genuinely reversible is what
lets the prompt be rare enough to still mean something when it appears.

Reversibility here is conditional, and the conditions are checked rather than
assumed: bubblewrap must exist, user namespaces must work, and the workspace
must be small enough to copy. If any of those fails the tool degrades back to
irreversible and the human is asked — which is the safe direction.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Copying the workspace is what buys reversibility, so it has to stay cheap
# enough to do before every command. Past this, we stop claiming reversibility
# rather than silently taking a slow path.
MAX_SNAPSHOT_BYTES = 200 * 1024 * 1024
MAX_SNAPSHOT_FILES = 20_000

# Directories that are large, regenerable, and not worth copying. Excluding
# them is a real limitation, not a free win: a command that corrupts
# node_modules is not undone by this snapshot. Stated in `Sandbox.caveats`.
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__",
             ".mypy_cache", ".pytest_cache", "target", "dist", "build",
             ".next", ".tox", ".gradle"}

# The toolchain the command is allowed to see. Everything not listed here is
# simply absent inside the sandbox — including /home, /root, /mnt and /media,
# which is the point: no credentials, no other projects, no SSH keys.
SYSTEM_PATHS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/lib32",
                "/etc/alternatives", "/etc/ssl", "/etc/ca-certificates",
                "/etc/passwd", "/etc/group", "/etc/nsswitch.conf",
                "/etc/resolv.conf", "/etc/localtime", "/opt")


@dataclass
class SandboxReport:
    """Why the sandbox is or is not usable, in terms a user can act on."""

    available: bool
    backend: str
    reason: str
    network: bool = False

    def __str__(self) -> str:
        if self.available:
            net = "network on" if self.network else "no network"
            return f"{self.backend} ({net})"
        return f"unavailable — {self.reason}"


def detect(allow_network: bool = False) -> SandboxReport:
    """Probe for a working sandbox rather than trusting that the binary exists.

    Presence of `bwrap` is not enough: on hosts where unprivileged user
    namespaces are disabled it is installed and non-functional. The probe runs
    an actual confined command, because a sandbox that has never been executed
    is an assumption.
    """
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return SandboxReport(False, "none", "bubblewrap (bwrap) is not installed")

    probe = subprocess.run(
        [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
         "--unshare-all", "--die-with-parent", "true"],
        capture_output=True, text=True, timeout=20, check=False)
    if probe.returncode != 0:
        detail = (probe.stderr or "").strip().splitlines()
        return SandboxReport(False, "bwrap",
                             detail[-1] if detail else "bwrap probe failed")
    return SandboxReport(True, "bwrap", "ok", network=allow_network)


def measure(root: Path) -> tuple[int, int]:
    """Total bytes and file count under `root`, ignoring SKIP_DIRS."""
    total = files = 0
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.is_file() and not path.is_symlink():
            try:
                total += path.stat().st_size
            except OSError:
                continue
            files += 1
            if files > MAX_SNAPSHOT_FILES or total > MAX_SNAPSHOT_BYTES:
                break
    return total, files


def snapshottable(root: Path) -> tuple[bool, str]:
    """Whether this workspace is small enough to copy before every command."""
    total, files = measure(root)
    if files > MAX_SNAPSHOT_FILES:
        return False, f"{files}+ files exceeds the {MAX_SNAPSHOT_FILES} snapshot limit"
    if total > MAX_SNAPSHOT_BYTES:
        return False, f"{total // (1024*1024)} MB exceeds the {MAX_SNAPSHOT_BYTES // (1024*1024)} MB snapshot limit"
    return True, f"{files} files, {total // 1024} KB"


class Sandbox:
    """Runs a command confined to one directory, and can put that directory back."""

    def __init__(self, root: Path, *, report: SandboxReport) -> None:
        self.root = root.resolve()
        self.report = report
        self._snap_dir: Path | None = None

    # -- confinement ------------------------------------------------------
    def _argv(self, command: str) -> list[str]:
        """Build the bwrap invocation.

        System directories are bound from an allowlist rather than by
        read-only-binding all of `/`. Binding everything is the obvious
        approach and it leaves a real hole: the command can still *read* the
        host — `~/.ssh/id_rsa`, `~/.aws/credentials`, another project's `.env`
        — and copy what it finds into the workspace. The agent then reads the
        workspace and sends it to the model. Cutting the network does not
        close that path, because the exfiltration route is the agent itself.

        So the command sees the toolchain and nothing else. `$HOME` is a
        tmpfs, not the real one.

        `--unshare-all` drops the network along with every other namespace;
        `--share-net` adds it back only when explicitly asked for.
        """
        argv = ["bwrap"]
        for d in SYSTEM_PATHS:
            if Path(d).exists():
                argv += ["--ro-bind", d, d]
        argv += [
            "--dev", "/dev",
            "--proc", "/proc",
            "--tmpfs", "/tmp",
            # A writable HOME the command can scribble in, which is not the
            # user's. Many toolchains fail outright without one.
            "--tmpfs", str(Path.home()),
            # Bound after the tmpfs mounts so a workspace living under /tmp or
            # under $HOME is not shadowed by them — an ordering bug that hands
            # the command an empty directory instead of the project.
            "--bind", str(self.root), str(self.root),
            "--unshare-all",
            "--die-with-parent",
            # A new session detaches the controlling terminal, closing the
            # TIOCSTI trick for pushing characters into the parent's tty.
            "--new-session",
            "--chdir", str(self.root),
            "--setenv", "HOME", str(Path.home()),
        ]
        if self.report.network:
            argv.append("--share-net")
        argv += ["/bin/sh", "-c", command]
        return argv

    def run(self, command: str, timeout: int = 120) -> subprocess.CompletedProcess:
        if not self.report.available:
            raise RuntimeError(f"sandbox unavailable: {self.report.reason}")
        # check=False on purpose: a non-zero exit is information the model needs,
        # not an exception. A failing test is a normal result here.
        return subprocess.run(self._argv(command), capture_output=True,
                              text=True, timeout=timeout, check=False)

    # -- reversibility ----------------------------------------------------
    def snapshot(self) -> str:
        """Copy the workspace aside. This is what makes a confined command undoable."""
        dest = Path(tempfile.mkdtemp(prefix="retie-ws-"))
        shutil.copytree(
            self.root, dest / "ws",
            symlinks=True,
            ignore=shutil.ignore_patterns(*SKIP_DIRS),
            dirs_exist_ok=True,
        )
        self._snap_dir = dest
        return str(dest)

    def restore(self, token: str) -> str:
        """Put the workspace back to a snapshot.

        Skipped directories are deliberately left alone rather than deleted:
        removing a `node_modules` the snapshot never captured would be a
        destructive "undo", which is the opposite of the point.
        """
        src = Path(token) / "ws"
        if not src.is_dir():
            raise RuntimeError(f"no workspace snapshot at {token}")
        for path in list(self.root.rglob("*")):
            rel = path.relative_to(self.root)
            if any(part in SKIP_DIRS for part in rel.parts):
                continue
            if path.is_file() or path.is_symlink():
                path.unlink(missing_ok=True)
        shutil.copytree(src, self.root, symlinks=True, dirs_exist_ok=True)
        return f"restored {self.root} from snapshot"

    def caveats(self) -> list[str]:
        """Everything this sandbox does not protect, stated rather than implied."""
        out = [
            ("directories excluded from the snapshot are not restored: "
             f"{', '.join(sorted(SKIP_DIRS))}"),
            ("the snapshot captures the workspace only — changes the command "
             "makes to its own memory, to running processes, or to anything "
             "outside the workspace are not covered"),
        ]
        if self.report.network:
            out.append("network is ENABLED: anything read can be sent out, and "
                       "no filesystem snapshot undoes that")
        return out
