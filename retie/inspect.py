"""Looking at what the agent is about to write, before it writes it.

The gate up to now asked one question: can this be undone? That is the right
first question and it is not the only one. A write can be perfectly reversible
and still be something nobody should let through unremarked — a hardcoded
credential, a `verify=False` slipped into a TLS call, an auth decorator quietly
dropped. "You can undo it" is not "it is fine", and treating reversibility as
the whole of safety was a real gap.

So writes now carry a second dimension. Reversibility decides whether an action
is recoverable; inspection decides whether it is *advisable*. They are separate
axes and both belong in the record.

The part that is not just another linter
----------------------------------------
Pattern-matching new content for `password = "..."` is table stakes and every
scanner does it. The check with actual signal here is the **diff**: not what the
code now contains, but what it *stopped* containing. An agent that removes a
certificate check, deletes a permission test, or turns an assertion off has
weakened the system, and nothing in the new text looks wrong — the evidence is
in what is missing. That is also precisely what a hijacked agent does, because
disabling a control is the cheapest way to make an obstacle disappear.

Deliberately not attempted
--------------------------
No architecture graph, no cross-repo risk model, no accumulated corpus of
findings from other people's codebases. Those are the moat of a platform with a
large customer base, they are data rather than code, and pretending a few
hundred lines of regex is the same thing would be dishonest. What this does is
narrower and stated: catch the specific, checkable things an agent does to code
in front of it, and put them in the record either way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Suffixes worth inspecting. Everything else is data or prose, where these
# patterns produce noise rather than signal.
CODE_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rb", ".java",
                 ".php", ".cs", ".rs", ".sh", ".bash", ".yml", ".yaml", ".tf",
                 ".env", ".ini", ".cfg", ".toml", ".json"}


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str          # "high" | "medium" | "low"
    message: str
    line: int = 0
    excerpt: str = ""

    def __str__(self) -> str:
        where = f" (line {self.line})" if self.line else ""
        return f"[{self.severity}] {self.rule}{where}: {self.message}"


# --------------------------------------------------------------------------
# Content rules — things that are wrong on sight.
# --------------------------------------------------------------------------
# Kept deliberately narrow. A rule that fires on ordinary code teaches the user
# to wave findings through, and a check that is always waved through is worse
# than no check because it also costs attention.
_CONTENT: list[tuple[str, str, str, re.Pattern[str]]] = [
    ("hardcoded-secret", "high",
     "a credential appears to be written into source",
     re.compile(r"""(?ix)
        (?:password|passwd|secret|api[_-]?key|token|access[_-]?key|private[_-]?key)
        \s*[:=]\s*
        ["'][^"'\s${}]{8,}["']""")),
    ("aws-key", "high", "an AWS access key id is present",
     re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private-key-block", "high", "a private key block is present",
     re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("tls-verification-off", "high",
     "certificate verification is disabled, which makes TLS decorative",
     re.compile(r"""(?ix)
        verify\s*=\s*False
      | rejectUnauthorized\s*:\s*false
      | InsecureSkipVerify\s*:\s*true
      | check_hostname\s*=\s*False
      | ssl\._create_unverified_context""")),
    ("shell-injection-risk", "medium",
     "a shell is invoked with a constructed string",
     re.compile(r"""(?ix)
        subprocess\.(?:run|call|Popen|check_output)\([^)]*shell\s*=\s*True
      | os\.system\s*\(""")),
    ("sql-string-building", "medium",
     "SQL appears to be assembled by concatenation or interpolation",
     re.compile(r"""(?ix)
        (?:execute|executemany|query)\s*\(\s*
        (?:f["']|["'][^"']*["']\s*[+%]|["'][^"']*\{)""")),
    ("dynamic-eval", "medium",
     "code is being evaluated at runtime",
     re.compile(r"(?<![\w.])(?:eval|exec)\s*\(|new\s+Function\s*\(")),
]

# --------------------------------------------------------------------------
# Removal rules — things whose *absence* is the problem.
# --------------------------------------------------------------------------
# These fire when a line matching the pattern existed before and does not after.
# This is the diff-aware half, and the half that catches an agent quietly taking
# a guard down rather than adding something bad.
_REMOVALS: list[tuple[str, str, str, re.Pattern[str]]] = [
    ("auth-check-removed", "high",
     "an authentication or permission check was removed",
     re.compile(r"""(?ix)
        @(?:login_required|requires_auth|authenticated|permission_required)
      | \b(?:is_authenticated|check_permission|require_role|authorize)\s*\(
      | \bassert\s+.*\b(?:auth|permission|role|owner)\b""")),
    ("crypto-verification-removed", "high",
     "a signature or certificate check was removed",
     re.compile(r"""(?ix)
        \b(?:verify_signature|check_signature|validate_token|verify_cert)\s*\(
      | \bverify\s*=\s*True
      | check_hostname\s*=\s*True""")),
    ("validation-removed", "medium",
     "an input validation or sanitisation step was removed",
     re.compile(r"""(?ix)
        \b(?:validate|sanitize|sanitise|escape|clean)_?\w*\s*\(
      | \braise\s+ValidationError""")),
    ("test-disabled", "medium",
     "a test was skipped or disabled",
     re.compile(r"""(?ix)
        @(?:pytest\.mark\.)?skip
      | \bit\.skip\s*\(
      | \bdescribe\.skip\s*\(
      | \bxfail\b""")),
]


def _looks_like_placeholder(text: str) -> bool:
    """Filter the obvious non-secrets before they become noise."""
    low = text.lower()
    return any(m in low for m in (
        "example", "changeme", "your-", "xxx", "placeholder", "dummy",
        "<", "${", "{{", "redacted", "fake", "test-", "sample"))


def inspect_content(path: str, content: str) -> list[Finding]:
    """Rules that judge the new text on its own."""
    if not any(path.endswith(s) for s in CODE_SUFFIXES):
        return []
    out: list[Finding] = []
    lines = content.splitlines()
    for rule, severity, message, pattern in _CONTENT:
        for i, line in enumerate(lines, 1):
            m = pattern.search(line)
            if not m:
                continue
            if rule in ("hardcoded-secret", "aws-key") and _looks_like_placeholder(line):
                continue
            out.append(Finding(rule, severity, message, i, line.strip()[:90]))
            break          # one finding per rule is enough to stop and look
    return out


def inspect_removal(path: str, before: str, after: str) -> list[Finding]:
    """Rules that judge what stopped being there.

    Counted per rule across the whole file rather than compared line by line.
    The line-set version was tried first and was too noisy to ship: appending a
    `timeout=5` to a call that still said `verify=True` changed the line, so the
    line "disappeared" and the rule fired on a completely benign edit. A check
    that cries wolf on ordinary edits gets waved through, and a check that is
    always waved through costs attention while protecting nothing.

    Counting matches instead asks the question that was actually meant: does
    this file still have as many of these guards as it did? An in-place edit
    keeps the count and stays silent; deleting one of three auth checks still
    drops the count and still fires.
    """
    if not any(path.endswith(s) for s in CODE_SUFFIXES):
        return []

    out: list[Finding] = []
    for rule, severity, message, pattern in _REMOVALS:
        was, now = len(pattern.findall(before)), len(pattern.findall(after))
        if now < was:
            sample = next((ln.strip() for ln in before.splitlines()
                           if pattern.search(ln)), "")
            out.append(Finding(
                rule, severity,
                f"{message} ({was} → {now})", 0, sample[:90]))
    return out


def inspect_write(path: str, content: str, prior: str | None = None) -> list[Finding]:
    """Everything worth saying about a write, before it happens."""
    findings = inspect_content(path, content)
    if prior:
        findings += inspect_removal(path, prior, content)
    return findings


def worst(findings: list[Finding]) -> str:
    for level in ("high", "medium", "low"):
        if any(f.severity == level for f in findings):
            return level
    return "none"


def summarise(findings: list[Finding]) -> str:
    if not findings:
        return "nothing flagged"
    by = {}
    for f in findings:
        by.setdefault(f.severity, []).append(f.rule)
    return "; ".join(f"{k}: {', '.join(sorted(set(v)))}" for k, v in by.items())
