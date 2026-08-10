# retie

A coding agent for the terminal that classifies every action by **how hard it is to undo**, plans the undo before acting, and records what it did to a tamper-evident ledger.

It works the way Claude Code works: you talk to it in a terminal, it reads and edits files in your project, runs your tests. The difference is what sits between the model and your filesystem.

```bash
pipx install retie
export OLLAMA_API_KEY=...           # https://ollama.com/settings/keys
retie run ./my-project
```

It runs on **hosted open models by default** — nothing downloaded, no GPU needed. `retie models` lists what Ollama Cloud is serving; `--provider anthropic` switches to Claude.

## Why

Prompt injection is not solved. Independent testing puts a widely-used agent runtime at [57% injection robustness](https://arxiv.org/pdf/2603.11619), and the strongest published conclusion on the topic is that the boundary that matters is not the model:

> "Once an agent can browse untrusted content and act externally, the relevant security boundary is its action boundary, not the model itself."
> — [Promptfoo](https://www.promptfoo.dev/blog/openclaw-at-work/)

Most agent runtimes gate on **tool name**. That has a documented hole its own authors are explicit about: allowing `exec` while denying `write` does not make the shell read-only, because the policy layer cannot see inside a shell command.

retie gates on **consequence** instead. The question is never "is this tool allowed?" — it is "can this be undone, and if not, who decided?"

## What that looks like

| Action | Reversibility | What happens |
|---|---|---|
| `read`, `glob`, `grep` | reversible (no effect) | runs, no prompt |
| `write`, `edit` | reversible — prior contents captured first | runs, no prompt, undoable |
| `bash` (sandboxed) | **reversible** — workspace snapshotted, no network | runs, no prompt, undoable |
| `bash` (no sandbox) | **irreversible** | stops and asks a person |
| anything unclassified | unknown | stops and asks a person |

Note that `bash` appears twice. **Sandboxing changes the classification**, and for a real reason rather than a configuration preference: unconfined, a shell command can reach the whole filesystem and the network and no inverse can be written for it; confined to a directory that was copied first, with no network, its inverse is exactly one operation — put the directory back.

That is also the fix for approval fatigue. A gate that prompts on every shell command trains you to approve reflexively, and a control that is always approved is decoration. Making confined commands genuinely reversible is what keeps the prompt rare enough to still mean something.

`bash` is not on a deny list. It lands in the approval rule because nothing can state its inverse — and so would any tool added tomorrow that nobody wrote a rule for. The default effect is deny, so an unclassified tool is refused rather than quietly allowed.

Run `python demo.py` to see all of it without spending a model call.

## Models, and how the quota is stretched

The default provider is [Ollama Cloud](https://ollama.com): frontier-scale open models — `qwen3.5:397b`, `mistral-large-3:675b`, `kimi-k2.7-code`, `glm-5.2`, `deepseek-v4-pro`, `gpt-oss:120b` — running on their infrastructure. Nothing to download, no GPU. That matters: a 397B model will not run on a laptop, and a model small enough to run on one tends to lose the thread partway through an agent loop.

Reached over Ollama's OpenAI-compatible endpoint at `https://ollama.com/v1`. Worth noting because Ollama's own compatibility docs cover only the local server, and secondary sources claim the cloud is *not* OpenAI-compatible — it is; verified against the live endpoint, not inferred.

### Most of the catalogue is not free — run `retie probe` first

Ollama publishes 18 cloud models. On a free plan, **7 were accessible**; the other 11 answer `403 this model requires a subscription`. That includes every model you would pick from a benchmark table — `kimi-k2.7-code`, `glm-5.2`, `qwen3.5:397b`, `deepseek-v4-pro`.

```bash
retie probe        # one token each; entitlement is checked before generation, so refusals are free
```

Results are cached, so the ladder is built from models you can actually call rather than from the published list. A `403` during a session drops that model permanently — it is not a quota problem and never resolves by waiting. A **timeout is recorded as inconclusive, not as a denial**: a cold model that took too long to wake would otherwise be dropped forever on the strength of one slow request.

### Rotating models does not defeat a quota

The obvious design is "when one model is rate-limited, switch to another." **It does not work**, and it is worth knowing why before relying on it.

[Ollama's pricing page](https://ollama.com/pricing) says limits are **per plan**, not per model — session limits reset every 5 hours, weekly limits every 7 days. When the account's allowance is gone, every model is gone with it. A tool that cycles the whole list turns one clear message into eighteen failed requests.

What *does* buy working hours is the other half of the same page: **usage is weighted by how heavy the model is**, from level 1 for light models like `gpt-oss:20b` up to level 4 for `deepseek-v4-pro`. A tier-1 model costs roughly a quarter of a tier-4 one for the same call. So retie:

- **starts on the cheapest model and escalates only on failure** — never on a guess that a task looks hard
- **derives tiers from live model size** rather than a hard-coded table, anchored on the two models Ollama documents as level 1 and level 4, so a newly published model is tiered the day it appears
- **tells session limits and weekly caps apart.** A session limit escalates to the next model. A weekly cap stops immediately and says so, because trying the rest would be a lie that costs you four more failed requests before you learn the truth
- **remembers refusals across runs**, so a fresh session does not re-hit a model that just refused
- **caps spend on request**: `--max-tier 2` never touches the heavy models

```bash
retie usage                 # what you have spent, and the ladder
retie run ./proj --max-tier 2
```

`retie usage` reports **consumption, not remaining balance**. Ollama publishes neither the free tier's allowance nor a usage endpoint, so a percentage would mean inventing the denominator.

The default model is **`kimi-k2.7-code`** when you name one; otherwise the ladder starts at tier 1. Kimi is chosen for tool-calling *stability* rather than coding score — the strongest published agentic-loop evidence, 4,000+ tool calls sustained in one session. `glm-5.2` scores marginally higher on coding (87 vs 86) with 1M context. Those figures are vendor-run; treat them as directional.

## The ordering is the design

Every tool call takes one path:

```
classify → gate → plan the undo → record → execute → confirm
```

The undo plan and the ledger entry are both written **before** the action leaves. The only moment the pre-action state is knowable is before the action, and a record written afterwards can be lost by exactly the failure it exists to capture.

This is enforced structurally, not by convention, and it survives having two providers. Tool *definitions* live in `toolspec.py`; backends translate them into wire formats but **never execute anything**; `Agent._dispatch` is the single place a tool callable is invoked, through `plane.guard`. Adding a provider cannot add a way around the gate, because providers do not run tools at all.

`python test_loop.py` and `python test_routing.py` assert exactly that, against both wire shapes and against the failure modes weaker models actually produce — hallucinated tool names, malformed JSON arguments, refused approvals, and runaway loops.

## Undo

```python
plane.undo(action_id)   # reverse one action
plane.undo_all()        # reverse everything this session's delegation authorised
```

Authority is rooted in a person. The agent holds a scoped, time-limited delegation signed by the user, so `undo_all` walks the delegation subtree rather than replaying actions one at a time — a compromised session is contained as a unit.

A sandboxed shell command *is* undone — its workspace snapshot is the inverse. An unsandboxed one is not, and `undo_all` reports that as a skip rather than claiming success.

## Evidence

The ledger is hash-chained and verified from a **separate process**:

```bash
retie verify --ledger retie-ledger.db
```

A log you can only check from inside the process that wrote it is a log, not evidence.

## Built on

[revoco](https://github.com/rsh1k/revoco) supplies the control plane: reversibility classification, the consequence-aware gate, the reversal engine, the delegation chain, and the ledger. This repository is the terminal agent around it — the tool surface, the loop, and the classification of what each tool costs to undo.

Worth stating plainly: revoco's `PRA02` detector caught the first version of this code claiming `write` was reversible when the undo had not actually been wired up. It blocked the action rather than trusting the claim. That is the behaviour the whole design depends on, and it was found by running it, not by reading it.

## What this does not do

- **It does not stop prompt injection.** Nothing does. It constrains what a successful injection can reach.
- **The sandbox is bubblewrap, and it is not a VM.** Shell commands get their own namespaces, no network, a tmpfs `$HOME`, and a read-only allowlist of system paths — `/home`, `/root`, `/mnt` and `/media` are simply absent, so `~/.ssh/id_rsa` and other projects' `.env` files are unreachable. That last part matters more than it looks: without it a confined command can read a secret, write it into the workspace, and the agent then sends it to the model. Cutting the network does not close that path, because the exfiltration route is the agent itself.
- **No seccomp filter yet.** `Seccomp: 0` inside the sandbox — namespaces and mounts are enforced, syscalls are not filtered. That is the next hardening step.
- **File tools are not sandboxed**, only path-confined in-process. They cannot execute anything, so the exposure is different in kind, but it is not the same guarantee.
- **No supply-chain or rogue-agent coverage** (OWASP ASI04, ASI10). Out of scope for now rather than partially done.
- **`--network` re-opens exfiltration.** Needed for installs; when it is on, anything the command reads can leave, and no filesystem snapshot undoes that. The CLI says so at startup.
- **Approval fatigue is real.** If every `bash` prompt gets a reflexive yes, the gate is decoration. `--yes` exists for scripted runs and prints a warning, because a control that is always bypassed should say so.
- **Keys are per-session.** The human and agent keypairs are generated at startup, so the delegation chain proves integrity within a session but does not yet carry identity across them.

## Status

Published to PyPI as [`retie`](https://pypi.org/project/retie/) — the name was free, checked including PyPI's case and separator folding. (`retie-agent` exists but is an MQTT/IPC library, unrelated domain.)

Releases are automatic: push to `main`, a patch version publishes via Trusted Publishing with no API token anywhere. See [docs/RELEASING.md](docs/RELEASING.md) — there is one manual PyPI form to fill in before the first release works.

- **Ollama Cloud path: verified live.** A full session on `gpt-oss:20b` read the file, edited it, and ran a shell command to check its own work. Streaming, tool-call reassembly, the gate, the sandbox, the ledger and usage accounting all held.
- **Anthropic path**: tested through the fake backend only, not against the live API.
- The safety plane, sandbox, undo and ledger are exercised without any key — `python demo.py`.

Three things the live run found that mocks could not:

1. **`gpt-oss` streams a separate `reasoning` field** beside `content`. It is kept out of the assistant text — feeding a model's own thinking-aloud back as dialogue teaches it that scratchpad is conversation — and surfaced dimmed instead. With a small `max_tokens` the reasoning consumes the whole budget and `content` comes back empty.
2. **Usage is absent from the stream unless you ask for it.** `stream_options: {"include_usage": true}` is required; without it the local accounting silently stayed at zero.
3. **Cheapest-first is not obviously right.** `gpt-oss:20b` fixed a one-line bug in **7 tool calls** — three globs and an `ls -R` to locate a file it had been given the name of. Seven tier-1 calls can cost more than two tier-3 calls. The ladder still starts cheap, but that is now a stated assumption rather than a proven one, and it is the next thing worth measuring.

The ledger earned its place here. The model claimed *"verified with a test call that outputs 5"* — the ledger showed it really did run `python -c 'import calc…'`. A model's account of its own work is checkable against an independent record.
