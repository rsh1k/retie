"""Talking to a model, without the rest of the program caring which one.

Two backends, one contract: take the conversation, stream assistant text out,
and hand back any tool calls the model asked for. Neither backend runs a tool —
that stays with the caller, which routes every call through the safety plane.
Keeping execution out of here is what stops a second provider quietly becoming
a second, ungated path to the filesystem.

Ollama Cloud is reached over its OpenAI-compatible surface at
`https://ollama.com/v1`. That is worth stating because Ollama's own
OpenAI-compatibility page documents only the local server, and at least one
secondary source claims the cloud is not OpenAI-compatible. It is: the model
list returns an OpenAI-shaped body and the chat endpoint returns
OpenAI-shaped errors. Verified against the live endpoint, not inferred.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .routing import LimitKind, Router, classify_limit
from .toolspec import ToolSpec

ANTHROPIC_MODEL = "claude-opus-5"
OLLAMA_BASE_URL = "https://ollama.com/v1"

# Ollama Cloud runs these; nothing is downloaded. Ordered by how they behave in
# an agent loop rather than by coding score, because a model that writes good
# code but loses the thread after twenty tool calls is useless here.
#
# Published figures are vendor-run and should be treated as directional.
# Notes are ours. Which of these an account may actually call is discovered at
# runtime, not listed here — on a free plan most of the catalogue returns 403,
# and hard-coding an entitlement guess would be wrong for every other reader.
OLLAMA_MODELS = {
    "gpt-oss:20b": "smallest and fastest; verified working on a free plan. Emits a "
                   "separate `reasoning` field, and needs a generous max_tokens or "
                   "the reasoning consumes the whole budget and content comes back "
                   "empty.",
    "gpt-oss:120b": "OpenAI's open-weight model, built with tool use in mind.",
    "nemotron-3-super": "the heaviest model observed accessible without a subscription.",
    "kimi-k2.7-code": "code-specialised, strongest published agentic-loop stability "
                      "(4,000+ tool calls in one session) — but subscription-only.",
    "glm-5.2": "marginally stronger on coding benchmarks, 1M context. Subscription-only.",
    "deepseek-v4-pro": "strong reasoning. Subscription-only.",
}
DEFAULT_OLLAMA_MODEL = "gpt-oss:20b"


class ProviderRefused(RuntimeError):
    """The provider declined this call for a quota or throttling reason."""

    def __init__(self, kind: str, detail: str, retry_after: float | None) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.retry_after = retry_after


class QuotaExhausted(RuntimeError):
    """No model on the ladder can serve this call.

    Carried separately from a generic error so the CLI can say *why* and when
    it resets, instead of printing a stack trace at someone who just wants to
    know whether to wait five hours or seven days.
    """

    def __init__(self, kind: str, detail: str, tried: list[str]) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.tried = tried


def _key_from_config() -> str | None:
    """Read the key from ~/.retie/config.json.

    An environment variable only exists in the shell that exported it, which
    makes it a poor place for a credential you want every terminal — and every
    tool that shells out — to find. The file is checked after the environment
    so an explicit export still wins.
    """
    import json
    import os
    import stat
    from pathlib import Path

    path = Path(os.environ.get("RETIE_CONFIG", "~/.retie/config.json")).expanduser()
    if not path.is_file():
        return None
    try:
        mode = path.stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            # Warn rather than refuse: a readable key is the user's call, but
            # they should know the file is not private.
            print(f"warning: {path} is readable by others; chmod 600 it")
        return (json.loads(path.read_text()) or {}).get("ollama_api_key") or None
    except (OSError, json.JSONDecodeError):
        return None


def _retry_after(headers: dict[str, str]) -> float | None:
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


@dataclass
class ToolCall:
    """A tool the model wants run. Provider-neutral."""

    id: str
    name: str
    args: dict[str, Any]


@dataclass
class Turn:
    """One assistant turn: what it said, and what it wants to run next."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None
    reasoning: str = ""


class AnthropicBackend:
    """Claude via the Messages API."""

    name = "anthropic"

    def __init__(self, model: str = ANTHROPIC_MODEL, *, effort: str = "high") -> None:
        import anthropic

        self.client = anthropic.Anthropic()
        self.model = model
        self.effort = effort

    def describe(self) -> str:
        return f"{self.model} (effort {self.effort})"

    def turn(self, system: str, messages: list[dict[str, Any]],
             specs: list[ToolSpec], on_text: Callable[[str], None]) -> Turn:
        with self.client.messages.stream(
            model=self.model,
            max_tokens=64000,
            system=system,
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
            tools=[s.anthropic() for s in specs],
            messages=messages,
        ) as stream:
            for event in stream:
                if (event.type == "content_block_delta"
                        and getattr(event.delta, "type", "") == "text_delta"):
                    on_text(event.delta.text)
            message = stream.get_final_message()

        calls = [ToolCall(b.id, b.name, dict(b.input))
                 for b in message.content if b.type == "tool_use"]
        text = "".join(b.text for b in message.content if b.type == "text")
        return Turn(text=text, tool_calls=calls, raw=message.content)

    def assistant_message(self, turn: Turn) -> dict[str, Any]:
        return {"role": "assistant", "content": turn.raw}

    def tool_results(self, results: list[tuple[ToolCall, str]]) -> dict[str, Any]:
        return {"role": "user",
                "content": [{"type": "tool_result", "tool_use_id": c.id, "content": out}
                            for c, out in results]}


class OpenAICompatBackend:
    """Any OpenAI-compatible chat endpoint. Used here for Ollama Cloud.

    The loop is written by hand rather than borrowed from a helper because the
    caller must be able to interpose on every tool call. A convenience runner
    that executes tools itself would put the filesystem one layer below the
    gate, which is the exact arrangement this project exists to avoid.
    """

    name = "ollama"

    def __init__(self, model: str = DEFAULT_OLLAMA_MODEL, *,
                 base_url: str = OLLAMA_BASE_URL, api_key: str | None = None,
                 router: Router | None = None,
                 on_switch: Callable[[str, str, str], None] | None = None,
                 on_reasoning: Callable[[str], None] | None = None) -> None:
        import os

        import httpx

        key = api_key or os.environ.get("OLLAMA_API_KEY") or _key_from_config()
        if not key:
            raise RuntimeError(
                "no Ollama API key. Get one at https://ollama.com/settings/keys, "
                "then either put it in ~/.retie/config.json as "
                '{"ollama_api_key": "..."} or export OLLAMA_API_KEY.')
        self.router = router
        self.on_switch = on_switch
        self.on_reasoning = on_reasoning
        self.model = model
        if router is not None:
            first = router.first()
            if first is not None:
                self.model = first.name
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=httpx.Timeout(300.0, connect=15.0),
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"})

    def describe(self) -> str:
        return f"{self.model} via {self.base_url}"

    def turn(self, system: str, messages: list[dict[str, Any]],
             specs: list[ToolSpec], on_text: Callable[[str], None]) -> Turn:
        """Run one turn, moving down the ladder if the provider refuses.

        A refusal is not retried blindly. `classify_limit` decides whether the
        refusal is worth reacting to at all: a weekly cap is account-wide, so
        the router returns no alternative and the error surfaces immediately
        rather than after eighteen identical failures.
        """
        if self.router is None:
            return self._attempt(self.model, system, messages, specs, on_text)

        tried: list[str] = []
        model = self.model
        while model:
            try:
                turn = self._attempt(model, system, messages, specs, on_text)
                self.model = model
                return turn
            except ProviderRefused as exc:
                tried.append(model)
                if exc.kind == LimitKind.FORBIDDEN:
                    # Not a quota problem and it never resolves by waiting, so
                    # the model leaves the ladder for good rather than being
                    # retried on every future session.
                    self.router.usage.mark_forbidden(model)
                else:
                    self.router.usage.block(model, exc.retry_after or 300, exc.kind)
                nxt = self.router.next_after(model, kind=exc.kind)
                if nxt is None:
                    raise QuotaExhausted(exc.kind, exc.detail, tried) from exc
                if self.on_switch:
                    self.on_switch(model, nxt.name, exc.kind)
                model = nxt.name
        raise QuotaExhausted(LimitKind.UNKNOWN, "no model available", tried)

    def _attempt(self, model: str, system: str, messages: list[dict[str, Any]],
                 specs: list[ToolSpec], on_text: Callable[[str], None]) -> Turn:
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}] + messages,
            "tools": [s.openai() for s in specs],
            "stream": True,
            # Without this the stream carries no usage at all — verified against
            # the live endpoint, where the default response has zero usage
            # chunks and the local accounting silently stayed at zero.
            "stream_options": {"include_usage": True},
        }
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        # Tool-call arguments arrive as JSON fragments spread across deltas and
        # keyed by index, so they are reassembled per index before parsing.
        partial: dict[int, dict[str, Any]] = {}

        with self._client.stream("POST", f"{self.base_url}/chat/completions",
                                 json=body) as response:
            if response.status_code != 200:
                detail = response.read().decode("utf-8", "replace")[:400]
                headers = {k.lower(): v for k, v in response.headers.items()}
                if response.status_code in (429, 402, 403, 503):
                    raise ProviderRefused(
                        classify_limit(response.status_code, detail, headers),
                        detail, _retry_after(headers))
                raise RuntimeError(f"{self.base_url} returned "
                                   f"{response.status_code}: {detail}")
            for line in response.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage") and self.router is not None:
                    u = chunk["usage"]
                    info = self.router.all.get(model)
                    if info is not None:
                        self.router.usage.record(
                            info, u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
                choices = chunk.get("choices") or [{}]
                delta = choices[0].get("delta") or {}
                if delta.get("content"):
                    text_parts.append(delta["content"])
                    on_text(delta["content"])
                if delta.get("reasoning"):
                    # gpt-oss models stream their scratchpad in a `reasoning`
                    # field beside `content`. It is kept out of the assistant
                    # text — sending it back as conversation would teach the
                    # model that its own thinking-aloud is dialogue — but it is
                    # captured so a caller can display it the way an IDE shows
                    # a thinking pane.
                    reasoning_parts.append(delta["reasoning"])
                    if self.on_reasoning:
                        self.on_reasoning(delta["reasoning"])
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    slot = partial.setdefault(idx, {"id": "", "name": "", "args": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["args"] += fn["arguments"]

        calls = []
        for idx in sorted(partial):
            slot = partial[idx]
            if not slot["name"]:
                continue
            try:
                args = json.loads(slot["args"]) if slot["args"].strip() else {}
            except json.JSONDecodeError:
                # Malformed arguments are reported to the model as a failed call
                # rather than crashing the session — smaller models do emit them,
                # and a model that is told what went wrong usually retries well.
                args = {"__parse_error__": slot["args"][:400]}
            calls.append(ToolCall(slot["id"] or f"call_{idx}", slot["name"], args))

        return Turn(text="".join(text_parts), tool_calls=calls,
                    reasoning="".join(reasoning_parts))

    def assistant_message(self, turn: Turn) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": turn.text or None}
        if turn.tool_calls:
            msg["tool_calls"] = [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name, "arguments": json.dumps(c.args)}}
                for c in turn.tool_calls]
        return msg

    def tool_results(self, results: list[tuple[ToolCall, str]]) -> Any:
        # OpenAI's shape is one message per result, not one message holding all
        # of them. Returned as a list and flattened by the caller.
        return [{"role": "tool", "tool_call_id": c.id, "content": out}
                for c, out in results]


def make_backend(kind: str, model: str | None, *, effort: str = "high",
                 api_key: str | None = None, router: Router | None = None,
                 on_switch: Callable[[str, str, str], None] | None = None,
                 on_reasoning: Callable[[str], None] | None = None) -> Any:
    if kind == "anthropic":
        return AnthropicBackend(model or ANTHROPIC_MODEL, effort=effort)
    if kind == "ollama":
        return OpenAICompatBackend(model or DEFAULT_OLLAMA_MODEL, api_key=api_key,
                                   router=router, on_switch=on_switch,
                                   on_reasoning=on_reasoning)
    raise ValueError(f"unknown provider {kind!r}")
