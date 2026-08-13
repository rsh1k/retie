"""Tools defined once, in a form both provider wire formats can be built from.

Previously the tool surface was written directly against the Anthropic SDK's
decorator, which meant the tool *definitions* and the *provider* were the same
object. Adding a second provider made that untenable — and the way it fails is
the part worth guarding against: two parallel tool lists drift, and a tool that
exists on one path but not the other is a hole in the gate rather than a
missing feature.

So there is one list. Each provider adapts it; neither owns it.

Every spec carries its own JSON Schema rather than deriving one from a Python
signature, because the two providers disagree about what a valid schema looks
like and an inferred one hides that disagreement until a call fails at runtime.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .tools import (
    Workspace,
    edit_file,
    glob_files,
    grep_files,
    read_file,
    run_bash,
    write_file,
)


@dataclass(frozen=True)
class ToolSpec:
    """One tool: what to call it, what it takes, and how to run it."""

    name: str
    description: str
    schema: dict[str, Any]
    run: Callable[..., str]

    def anthropic(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "input_schema": self.schema}

    def openai(self) -> dict[str, Any]:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.schema}}


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


def _str(desc: str) -> dict[str, Any]:
    return {"type": "string", "description": desc}


def build_specs(ws: Workspace, sandbox: Any = None) -> list[ToolSpec]:
    """The tool surface. Descriptions state *when* to use each tool.

    Open models tend to under-call tools that only say what they do, so each
    description names the situation that should trigger it. That guidance costs
    nothing on a strong model and materially changes behaviour on a weaker one.
    """
    return [
        ToolSpec(
            "read",
            "Read a file from the project. Call this before editing anything, "
            "so your edit matches the file exactly.",
            _obj({"path": _str("Path relative to the project root.")}, ["path"]),
            lambda path: read_file(ws, path),
        ),
        ToolSpec(
            "write",
            "Create a file, or replace one entirely. To change part of an "
            "existing file prefer `edit` — it is smaller and easier to review.",
            _obj({"path": _str("Path relative to the project root."),
                  "content": _str("The complete new contents of the file.")},
                 ["path", "content"]),
            lambda path, content: write_file(ws, path, content),
        ),
        ToolSpec(
            "edit",
            "Replace one exact span of text in a file. `old` must appear "
            "exactly once — include surrounding lines to make it unique.",
            _obj({"path": _str("Path relative to the project root."),
                  "old": _str("Text to replace. Must match the file byte for byte."),
                  "new": _str("Replacement text.")},
                 ["path", "old", "new"]),
            lambda path, old, new: edit_file(ws, path, old, new),
        ),
        ToolSpec(
            "glob",
            "List files matching a glob pattern such as `**/*.py`. Call this to "
            "find your way around a project before searching contents.",
            _obj({"pattern": _str("Glob pattern relative to the project root.")},
                 ["pattern"]),
            lambda pattern: glob_files(ws, pattern),
        ),
        ToolSpec(
            "grep",
            "Search file contents with a regular expression. Call this to "
            "locate a symbol or string when you do not know which file holds it.",
            _obj({"pattern": _str("Python regular expression."),
                  "glob": _str("Restrict the search to files matching this glob.")},
                 ["pattern"]),
            lambda pattern, glob="*": grep_files(ws, pattern, glob),
        ),
        ToolSpec(
            "bash",
            "Run a shell command in the project directory — tests, builds, git. "
            "Where a sandbox is available the command is confined to the project "
            "with no network and the directory is snapshotted first, so it runs "
            "without interrupting the user; otherwise the user is asked first.",
            _obj({"command": _str("The command to run.")}, ["command"]),
            lambda command: run_bash(ws, command, sandbox=sandbox),
        ),
    ]


def recall_spec(store: Any) -> ToolSpec:
    """The tool that fetches a result the transcript elided.

    Registered as an ordinary tool so it passes the same gate as everything
    else. It reads nothing from disk and changes nothing, so it classifies as
    reversible and never prompts — but it is not a special case in the loop.
    """
    return ToolSpec(
        "recall",
        "Retrieve the full text of an earlier tool result that was truncated. "
        "Use this only when the preview genuinely is not enough — the preview "
        "usually answers the question.",
        _obj({"ref": _str('The reference from the truncation notice, e.g. "r1".')},
             ["ref"]),
        lambda ref: store.recall(ref),
    )
