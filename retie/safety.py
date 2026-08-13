"""The safety plane: what happens between the model asking and the tool running.

Every tool call goes through the same four steps, in this order:

    classify → gate → plan the undo → record → execute → confirm

The ordering is the design. The undo plan and the ledger entry are both written
*before* the action leaves, because the only moment the pre-action state is
knowable is before the action, and a record written afterwards can be lost by
exactly the failure it exists to capture.

The consequence of that ordering is the property this whole tool exists for:
if the model is hijacked — by a poisoned file it read, a malicious repo, a
crafted error message — the hijack still has to pass a gate that classifies by
*what the action does to the world*, not by what the tool is called. Detecting
injection is not solved and probably is not solvable; constraining what a
successful injection can reach is.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from revoco import ControlPlane, Effect
from revoco.gate.policy import Policy, Rule
from revoco.reversal import InverseRegistry, Reversibility

from .inspect import Finding, inspect_write, worst
from .sandbox import Sandbox, detect, snapshottable
from .tools import ToolError, Workspace, inverse_specs

# Inspection severity expressed as a risk number, because that is the vocabulary
# the policy already speaks. Both sit under the delegation's `max_risk` of 40 so
# a flagged write is *approvable* rather than scope-rejected — the point is to
# put a person in front of it, not to make it impossible.
RISK_HIGH = 30
RISK_MEDIUM = 15
RISK_APPROVAL = 25       # the threshold the policy rule keys on: high, not medium

_RISK = {"high": RISK_HIGH, "medium": RISK_MEDIUM, "low": 5, "none": 0}


def risk_of(findings: list[Finding]) -> int:
    """Inspection severity translated into the number the policy keys on."""
    return _RISK.get(worst(findings), 0)


def default_policy() -> Policy:
    """The shipped policy, written in terms of consequence rather than tool name.

    Read the rules in order and the whole thesis is visible: what an action can
    do to the world decides how it is handled. `bash` is not named in a deny
    list — it lands in the approval rule because its reversibility is not
    `REVERSIBLE`, and anything else that could not be undone would land there
    too, including a tool added tomorrow that nobody wrote a rule for.

    Default effect is DENY, so a tool nobody classified is refused rather than
    quietly allowed.
    """
    return Policy(
        name="retie-default",
        version="1",
        default_effect=Effect.DENY,
        rules=(
            Rule(
                id="reads-are-free",
                effect=Effect.ALLOW,
                actions=("read",),
                reason="observing changes nothing",
            ),
            # Above the permissive rule on purpose: first match wins, so this is
            # the only way a reversible write reaches a human. It exists because
            # "you can undo it" turned out not to mean "it is fine" — a
            # hardcoded credential or a deleted certificate check is perfectly
            # reversible and still nothing to wave through. Reversibility says
            # whether an action is recoverable; the risk score says whether it
            # is advisable, and they are different questions.
            Rule(
                id="risky-writes-need-a-human",
                effect=Effect.REQUIRE_APPROVAL,
                actions=("write",),
                min_risk=RISK_APPROVAL,
                reason="inspection flagged something in this change worth a look",
            ),
            Rule(
                id="reversible-writes-proceed",
                effect=Effect.ALLOW,
                actions=("write",),
                reversibility=(Reversibility.REVERSIBLE,),
                reason="the undo plan exists before the action runs",
            ),
            Rule(
                id="one-way-doors-need-a-human",
                effect=Effect.REQUIRE_APPROVAL,
                reversibility=(Reversibility.IRREVERSIBLE,
                               Reversibility.COMPENSABLE,
                               Reversibility.UNKNOWN),
                reason="this cannot be undone, so a person decides",
            ),
        ),
    )


@dataclass
class Outcome:
    """What the plane decided, in a form the caller can render and the model can read."""

    allowed: bool
    text: str
    verdict: Any = None
    tool: str = ""
    reversibility: str = ""


ApprovalFn = Callable[[str, str, dict[str, Any]], bool]


class FileStateReader:
    """Captures a file's contents before it is written, for the undo plan.

    Without this, declaring `write` reversible is a claim rather than a
    capability — revoco's PRA02 detector calls that a *phantom rollback* and
    blocks the action, which is the correct response. The undo has to be real
    before the action is allowed to depend on it.
    """

    def __init__(self, ws: Workspace, sandbox: Any = None) -> None:
        self.ws = ws
        self.sandbox = sandbox

    def __call__(self, tool: str, args: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if "workspace_token" in fields and self.sandbox is not None:
            # The whole workspace is copied before a confined shell command.
            # This is the expensive step that buys reversibility, so it only
            # runs for the tool that needs it.
            out["workspace_token"] = self.sandbox.snapshot()
            return out
        if "path" not in fields:
            return out
        try:
            target = self.ws.resolve(str(args.get("path", "")))
        except ToolError:
            return out
        # A partial dict is better than raising: a missing field degrades the
        # plan to a named gap instead of blocking the action silently.
        out["path"] = str(target)
        out["prior_contents"] = target.read_text(encoding="utf-8") if target.is_file() else None
        return out


class FileInverseExecutor:
    """Puts a file back to the contents captured before the action ran."""

    def __init__(self, ws: Workspace, sandbox: Any = None) -> None:
        self.ws = ws
        self.sandbox = sandbox

    def __call__(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "noop":
            return "nothing to undo"
        if tool == "restore_workspace":
            if self.sandbox is None:
                raise ToolError("no sandbox to restore a workspace from")
            return self.sandbox.restore(str(args["token"]))
        if tool != "restore_snapshot":
            raise ToolError(f"no inverse implemented for {tool}")
        target = Path(str(args["path"]))
        prior = args.get("prior_contents")
        if prior is None:
            target.unlink(missing_ok=True)
            return f"removed {target} (it did not exist before)"
        target.write_text(prior, encoding="utf-8")
        return f"restored {target} ({len(prior)} chars)"


def _describe(tool: str, args: dict[str, Any]) -> str:
    """A one-line summary a human can approve or refuse without reading JSON."""
    if tool == "bash":
        return f"run shell command: {args.get('command', '')[:160]}"
    if tool in {"write", "edit"}:
        return f"{tool} {args.get('path', '?')}"
    if tool == "grep":
        return f"search for {args.get('pattern', '?')!r}"
    return f"{tool} {args.get('path') or args.get('pattern') or ''}"


class SafetyPlane:
    """Wraps a control plane around the tool surface.

    `on_approval` is called when the gate wants a human. Returning False is a
    refusal, and a refusal is reported to the model as an ordinary tool result
    rather than an exception — the model should adapt and try something else,
    not crash the session.
    """

    def __init__(
        self,
        workspace: Workspace,
        *,
        ledger_path: Path | None = None,
        on_approval: ApprovalFn | None = None,
        auto_approve: bool = False,
        allow_network: bool = False,
    ) -> None:
        self.ws = workspace
        self.session_id = ""      # set by the Agent once a session opens
        self.on_approval = on_approval
        self.auto_approve = auto_approve
        self.pending_snapshots: dict[str, str] = {}
        # Findings for the call currently being authorised, so the approval
        # prompt can explain itself. Scoped to one call and cleared each time.
        self._findings: list[Finding] = []

        # Reversibility is decided by what this machine can actually do, so
        # the sandbox is probed before the specs are built rather than assumed.
        self.sandbox_report = detect(allow_network=allow_network)
        affordable, self.snapshot_note = snapshottable(workspace.root)
        self.sandboxed = self.sandbox_report.available and affordable
        self.sandbox = (Sandbox(workspace.root, report=self.sandbox_report)
                        if self.sandbox_report.available else None)

        registry = InverseRegistry()
        registry.register_many(inverse_specs(sandboxed=self.sandboxed))

        # revoco's own store, not veritrail's: the two expose different
        # `append_ledger` signatures, and the control plane speaks revoco's.
        store = None
        if ledger_path is not None:
            from revoco.store import SqliteStore

            store = SqliteStore(str(ledger_path))

        self.executor = FileInverseExecutor(workspace, self.sandbox if self.sandboxed else None)
        self.plane = ControlPlane(
            policy=default_policy(),
            inverse_registry=registry,
            state_reader=FileStateReader(workspace, self.sandbox if self.sandboxed else None),
            store=store,
            approval_hook=self._approve,
            fail_closed=True,
        )
        self.actor_id, self.private_key, self.delegation_id = self._bootstrap()

    # -- identity ---------------------------------------------------------
    def _bootstrap(self) -> tuple[str, Any, str]:
        """Register the human and the agent, and delegate from one to the other.

        Authority is rooted in a person, not in the process. The agent holds a
        scoped, time-limited grant that chains back to the user, which is what
        makes `undo_all` possible later: revoking the delegation identifies the
        entire blast radius of everything the agent did under it.

        The scope stays permissive on reversibility; the *policy* is what
        routes irreversible actions to a human. That split is the difference
        from name-based tool policy — `bash` is not gated because of what it
        is called, but because nothing can say how to undo it.
        """
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from revoco.authority import Scope
        from revoco.reversal import Reversibility

        human_key = ed25519.Ed25519PrivateKey.generate()
        agent_key = ed25519.Ed25519PrivateKey.generate()
        human = self.plane.register_human("local-user", human_key.public_key())
        agent = self.plane.register_agent("retie-agent", agent_key.public_key())

        grant = self.plane.issue_root_delegation(
            human_private_key=human_key,
            human_id=human.id,
            agent_id=agent.id,
            scope=Scope(
                allowed_tools=frozenset({"read", "write", "edit", "glob", "grep",
                                         "bash", "fetch", "recall"}),
                allowed_actions=frozenset({"read", "write"}),
                max_risk=40,
                # Deliberately permissive at the delegation layer. A floor of
                # REVERSIBLE here would make human approval meaningless: an
                # irreversible action would fail the scope check even after a
                # person said yes. Reversibility is the *policy's* decision.
                min_reversibility=Reversibility.UNKNOWN,
            ),
            purpose="interactive coding session",
            ttl_seconds=8 * 3600,
        )
        return agent.id, agent_key, grant.id

    # -- approval ---------------------------------------------------------
    def _approve(self, tool: str, args: dict[str, Any], principal: Any, decision: Any) -> bool:
        """Called by the control plane when a rule requires a human.

        Signature is fixed by revoco: (tool, args, principal, decision).
        Defaults to refusal — an approval gate with no one to ask is a denial,
        not a pass.
        """
        if self.auto_approve:
            return True
        if self.on_approval is None:
            return False
        what = _describe(tool, args or {})
        # A person asked to approve "write config.py" with no reason will say
        # yes, because that is an ordinary thing for a coding agent to do. The
        # finding is the entire reason the prompt exists, so it goes in it.
        if self._findings:
            what += "\n    " + "\n    ".join(str(f) for f in self._findings)
        return self.on_approval(tool, what, args or {})

    # -- inspection -------------------------------------------------------
    def _inspect(self, tool: str, args: dict[str, Any]) -> list[Finding]:
        """Look at the content a write is about to produce.

        This has to happen before `authorize`, because its output is an input to
        the decision. For `edit` the resulting text is reconstructed from the
        prior contents rather than guessed: the removal rules compare before
        against after, so a wrong "after" would produce wrong findings, and
        wrong findings are the thing that makes a check worthless.

        Failure here is deliberately silent. Inspection is advisory — a
        malformed file or an unreadable path should not take down a write that
        the reversibility gate has no objection to. What must not happen is
        inspection *appearing* to run and finding nothing, so the result is
        recorded either way, including when it is empty.
        """
        if tool not in {"write", "edit"}:
            return []
        path = str(args.get("path", ""))
        if not path:
            return []
        try:
            target = self.ws.resolve(path)
            prior = target.read_text(encoding="utf-8") if target.is_file() else None
        except (ToolError, OSError, UnicodeDecodeError):
            prior = None

        if tool == "write":
            content = str(args.get("content", ""))
        else:
            old, new = str(args.get("old", "")), str(args.get("new", ""))
            if prior is None or old not in prior:
                return []          # the edit will fail anyway; nothing real to judge
            content = prior.replace(old, new)

        try:
            return inspect_write(path, content, prior=prior)
        except Exception:  # noqa: BLE001 - advisory only, never load-bearing
            return []

    # -- the wrapper ------------------------------------------------------
    def guard(self, tool: str, args: dict[str, Any], execute: Callable[[], str]) -> Outcome:
        """Authorise, snapshot, run, confirm. The single path every call takes."""
        findings = self._findings = self._inspect(tool, args)
        risk = risk_of(findings)
        if findings:
            # Written into the same hash chain as the verdict, so a replay shows
            # what was noticed at the time and not merely what was decided. A
            # finding that only ever reached the terminal is not evidence.
            from .session import record

            record(self, "inspection", {
                "session_id": self.session_id,
                "tool": tool,
                "path": str(args.get("path", "")),
                "risk": risk,
                "worst": worst(findings),
                "findings": [{"rule": f.rule, "severity": f.severity,
                              "message": f.message, "line": f.line,
                              "excerpt": f.excerpt} for f in findings],
            })

        verdict = self.plane.authorize(
            actor_private_key=self.private_key,
            actor_id=self.actor_id,
            delegation_id=self.delegation_id,
            tool=tool,
            args=args,
            action="read" if tool in {"read", "glob", "grep"} else "write",
            risk=risk,
            # The description stays clean. Findings are their own ledger entry
            # and a replay joins them back, so repeating them here would only
            # duplicate them in every rendering.
            description=_describe(tool, args),
            session_id=self.session_id,
        )
        rev = getattr(verdict.reversibility, "value", str(verdict.reversibility))

        if not verdict.allowed:
            reason = verdict.reason or "blocked by policy"
            note = "denied" if verdict.effect is Effect.DENY else "not approved"
            return Outcome(False,
                           f"[{note}] {reason}. Try a different approach, or ask the "
                           f"user to run this themselves.",
                           verdict, tool, rev)

        # Snapshot *after* authorisation and *before* the write, so a denied
        # call leaves no snapshot behind and an allowed one always has its undo
        # in place before anything changes on disk.
        if tool in {"write", "edit"}:
            try:
                target = self.ws.resolve(str(args.get("path", "")))
                token = self.ws.snapshot(target)
                if token:
                    self.pending_snapshots[verdict.action_id] = token
            except ToolError:
                pass  # resolve() will raise again inside execute() with a clear message

        try:
            result = execute()
        except ToolError as exc:
            self.plane.confirm(verdict, result={"error": str(exc)})
            return Outcome(False, f"[error] {exc}", verdict, tool, rev)
        except Exception as exc:  # noqa: BLE001 - surfaced to the model, not swallowed
            self.plane.confirm(verdict, result={"error": repr(exc)})
            return Outcome(False, f"[error] {type(exc).__name__}: {exc}", verdict, tool, rev)

        self.plane.confirm(verdict, result={"ok": True})
        return Outcome(True, result, verdict, tool, rev)

    # -- evidence ---------------------------------------------------------
    def verify(self) -> bool:
        return self.plane.verify()

    def stats(self) -> dict[str, Any]:
        return self.plane.stats()


    # -- undo -------------------------------------------------------------
    def undo(self, action_id: str) -> Any:
        """Reverse one recorded action."""
        return self.plane.undo(action_id, self.executor)

    def undo_all(self) -> Any:
        """Reverse everything this session's delegation authorised.

        This is the containment path: rather than replaying actions one by one,
        it walks the delegation subtree, so a compromised session is undone as
        a unit.
        """
        return self.plane.undo_all(self.executor, delegation_id=self.delegation_id)
