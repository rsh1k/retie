# retie

A terminal coding agent that figures out how to undo an action before it takes it, and keeps a record you can check afterwards.

It works like Claude Code: you talk to it in a terminal, it reads and edits files in your project and runs your tests. What's different is the layer between the model and your filesystem.

```bash
pipx install retie
export OLLAMA_API_KEY=...           # https://ollama.com/settings/keys
retie run ./my-project
```

It defaults to hosted open models, so there's nothing to download and no GPU involved. `retie models` lists what Ollama Cloud is serving. `--provider anthropic` switches to Claude.

## Why bother

Prompt injection isn't solved. Independent testing puts a widely-used agent runtime at [57% injection robustness](https://arxiv.org/pdf/2603.11619), and the most useful conclusion I've read on it is that the model isn't where the boundary belongs:

> "Once an agent can browse untrusted content and act externally, the relevant security boundary is its action boundary, not the model itself."
> — [Promptfoo](https://www.promptfoo.dev/blog/openclaw-at-work/)

Most runtimes gate on tool name. There's a well-known hole in that, one their own authors are upfront about: allowing `exec` while denying `write` doesn't give you a read-only shell, because the policy layer can't see inside a shell command.

retie gates on consequence instead. The question isn't "is this tool allowed" but "can this be undone, and if not, who said yes?"

## How actions get classified

| Action | Reversibility | What happens |
|---|---|---|
| `read`, `glob`, `grep`, `fetch` | reversible (no effect) | runs, no prompt |
| `write`, `edit` | reversible, prior contents captured first | runs, no prompt, undoable |
| `bash` (sandboxed) | **reversible**, workspace snapshotted, no network | runs, no prompt, undoable |
| `bash` (no sandbox) | **irreversible** | stops and asks |
| anything unclassified | unknown | stops and asks |

`bash` shows up twice on purpose. Sandboxing genuinely changes the classification: an unconfined shell command can touch your whole filesystem and the network, and there's no sensible inverse for that. Confine it to a directory you copied first, cut the network, and the inverse is one operation — put the directory back.

That's also my answer to approval fatigue. If the gate prompts on every shell command you'll start approving reflexively, and at that point it's decoration. Making confined commands actually reversible is what keeps prompts rare enough to still mean something.

Note that `bash` isn't on a deny list anywhere. It ends up in the approval rule because nothing can state its inverse, and so would any tool added tomorrow that nobody wrote a rule for. Default effect is deny, so an unclassified tool gets refused rather than quietly let through.

`python demo.py` walks through all of this without spending a model call.

## Reversible isn't the same as safe

The gate above only asks whether something can be undone. That's the right first question but it isn't the only one. A hardcoded credential, a `verify=False` dropped into a TLS call, a permission decorator deleted: all trivially reversible, none of them things you want going through unremarked.

So writes get inspected too, on a separate axis:

| Written content | Risk | What happens |
|---|---|---|
| clean | 0 | runs, no prompt |
| `subprocess.run(cmd, shell=True)`, SQL built by interpolation | medium | logged, still runs |
| `API_KEY = "sk_live_…"`, `verify=False`, an auth check removed | high | stops and asks |

Reversibility tells you whether an action can be recovered. Inspection tells you whether it's a good idea. The risk score gets fed into the same policy engine as everything else rather than short-circuiting it, so the gate is still the only thing making decisions.

The part I actually care about is the diff. Grepping new text for `password = "..."` is table stakes and every scanner does it. The interesting check is what the code *stopped* containing:

```
edit pay.py
  ⚑ high: auth-check-removed          (1 → 0)
  ⚑ high: crypto-verification-removed (1 → 0)
```

Nothing in the new text looks wrong. The evidence is in what's missing, which is also what a hijacked agent tends to do, since removing a control is the cheapest way to make an obstacle go away.

Rules count matches per file rather than diffing line by line. I tried line-set comparison first and it was unusable: appending `timeout=5` to a call that still said `verify=True` changed the line, so the line "disappeared" and it reported a removed certificate check at high severity on a completely harmless edit. Counting asks the question I actually meant, which is whether the file still has as many of these guards as it had. An in-place edit keeps the count and stays quiet; dropping one of three auth checks still fires.

## Models and quota

The default provider is [Ollama Cloud](https://ollama.com), which serves frontier-scale open models (`qwen3.5:397b`, `mistral-large-3:675b`, `kimi-k2.7-code`, `glm-5.2`, `deepseek-v4-pro`, `gpt-oss:120b`) on their hardware. That matters more than it sounds: a 397B model won't run on a laptop, and models small enough to run on one tend to lose the plot halfway through an agent loop.

It talks to the OpenAI-compatible endpoint at `https://ollama.com/v1`. Worth flagging because Ollama's compatibility docs only cover the local server, and some secondary sources claim the cloud isn't OpenAI-compatible. It is. I checked against the live endpoint.

### Run `retie probe` first, most of the catalogue isn't free

Ollama publishes 18 cloud models. On a free plan I could reach 7 of them. The other 11 answer `403 this model requires a subscription`, and that includes every model you'd pick off a benchmark table: `kimi-k2.7-code`, `glm-5.2`, `qwen3.5:397b`, `deepseek-v4-pro`.

```bash
retie probe        # one token each; entitlement is checked before generation, so refusals cost nothing
```

Results get cached so the ladder is built from models you can actually call. A `403` mid-session drops that model permanently, since it's an entitlement problem and waiting won't fix it. Timeouts are recorded as inconclusive rather than as denials, because otherwise a cold model that took too long to wake up would get dropped forever on the strength of one slow request.

### Rotating models won't get you around a quota

The obvious design is "when one model is rate-limited, switch to another". It doesn't work, and it's worth knowing why before you build on it.

[Ollama's pricing page](https://ollama.com/pricing) says limits are per plan, not per model. Session limits reset every 5 hours, weekly limits every 7 days. Once the account's allowance is gone, every model is gone with it, and a tool that cycles the whole list just turns one clear error into eighteen failed requests.

What does buy you working hours is on the same page: usage is weighted by model size, level 1 for light models like `gpt-oss:20b` up to level 4 for `deepseek-v4-pro`. A tier-1 model costs roughly a quarter of a tier-4 one for the same call. So retie:

- **starts cheap and escalates only on failure**, never on a guess that a task looks hard. On the same task, tier 1 cost 4.97 quota units in 14s and tier 3 cost 15.78 in 395s. The heavy model was more efficient per call (3 tool calls vs 5), just not enough to beat the weight. Latency doesn't track weight at all, and the spread *within* tier 1 was wider than the gap between tiers 1 and 2, so treat tier as a budget guardrail and not a quality ranking.
- **derives tiers from live model size** instead of a hard-coded table, anchored on the two models Ollama documents as level 1 and level 4, so a new model gets tiered the day it appears.
- **tells session limits and weekly caps apart.** A session limit escalates to the next model. A weekly cap stops immediately and says so, because trying the rest would just cost you four more failed requests before you found out.
- **remembers refusals between runs**, so a fresh session doesn't re-hit a model that refused a minute ago.
- **caps spend on request**: `--max-tier 2` never touches the heavy models.

```bash
retie usage                 # what you've spent, and the ladder
retie run ./proj --max-tier 2
```

`retie usage` reports consumption, not remaining balance. Ollama publishes neither the free tier's allowance nor a usage endpoint, so any percentage would mean inventing the denominator.

If you name a model, the default is `kimi-k2.7-code`; otherwise the ladder starts at tier 1. I picked Kimi for tool-calling stability rather than coding score, on the back of the strongest agentic-loop evidence I could find (4,000+ tool calls sustained in one session). `glm-5.2` scores slightly higher on coding, 87 vs 86, with 1M context. Both figures are vendor-run, so take them as directional.

## The web tool

An agent that reads the web and writes to a filesystem is exactly the shape [Promptfoo demonstrated breaking](https://www.promptfoo.dev/blog/openclaw-at-work/): malicious page, agent reads it, agent does what the page said. So it belongs in a tool built around assuming the hijack lands and limiting what it can reach.

```bash
› read https://peps.python.org/pep-0621/ and check our pyproject against it
```

Three things it does that a naive fetch doesn't:

**The fence is random per fetch.** Untrusted text gets wrapped in a marker the content can't guess. Fixed markers are forgeable: content containing the marker closes the block and starts talking to the model from outside it. Not hypothetical, either. I ran into a production RAG pipeline fencing retrieved documents in a constant `"""` that page content could have emitted.

**SSRF is checked before the request goes out.** DNS gets resolved first and the resolved address is what's checked, since a public hostname can point anywhere. Redirects are followed by hand so every hop is re-checked, because a public URL that redirects to `127.0.0.1` is the standard way past a guard that only looks at the first request. Refused: loopback, private ranges, link-local (including `169.254.169.254`), IPv6 loopback, and anything that isn't http/https.

**The response is scanned and the finding recorded** whether or not anything gets blocked. Detection isn't the control here, the gate is, but an injection attempt that reached the model is the kind of thing you want in an audit trail later. The payload still gets delivered rather than stripped, because hiding it leaves the model reasoning from a gap.

There's a deliberate asymmetry worth noticing: a sandboxed shell has no network, this tool does. Fetching is read-only and reversible so it needs no approval, and it's also the one route by which untrusted text gets into a session.

## Token cost

Coding agents read files and files are long. Put every read straight into the transcript and it gets re-sent, and re-billed, on every later turn. On a metered plan that's the biggest avoidable cost and the thing that ends a long session early.

retie keeps large tool results out of the conversation, leaving a preview and a reference. The model calls `recall(ref=...)` if it wants the rest. Nothing is thrown away.

Measured on a task that reads two ~25 KB source files and summarises them:

| | Prompt tokens | Quota units | Tool calls | Time |
|---|---|---|---|---|
| compaction off | 18,862 | 19.25 | 6 | 15s |
| **on** | **6,646** | **7.09** | **5** | **12s** |

65% fewer prompt tokens, no extra calls, slightly faster. My first attempt used a 420-character preview and was considerably worse (11 calls instead of 6) because the preview was too small to answer from, so the model recalled nearly everything and paid the stub on top of the payload. Preview size turned out to be the whole game, and I tuned it by measuring rather than guessing.

These are single runs on one task, so treat the exact numbers as indicative. The direction and rough magnitude held across every configuration I tried.

## Ordering

Every tool call takes the same path:

```
classify → gate → plan the undo → record → execute → confirm
```

The undo plan and the ledger entry are both written before the action goes out. Pre-action state is only knowable before the action, and a record written afterwards can be lost by exactly the failure it exists to capture.

This is structural rather than a convention, and it survived adding a second provider. Tool definitions live in `toolspec.py`, backends translate them to wire formats but never execute anything, and `Agent._dispatch` is the only place a tool callable gets invoked, through `plane.guard`. Adding a provider can't add a way around the gate, because providers don't run tools.

`python test_loop.py` and `python test_routing.py` assert that against both wire shapes, plus the failure modes weaker models actually produce: hallucinated tool names, malformed JSON arguments, refused approvals, runaway loops.

## Undo

```python
plane.undo(action_id)   # reverse one action
plane.undo_all()        # reverse everything this session's delegation authorised
```

Authority is rooted in a person. The agent holds a scoped, time-limited delegation signed by the user, so `undo_all` walks the delegation subtree instead of replaying actions one at a time, and a compromised session gets contained in one go.

A sandboxed shell command does get undone, since its workspace snapshot is the inverse. An unsandboxed one doesn't, and `undo_all` reports that as a skip rather than claiming success.

## Replay

```bash
retie --ledger retie-ledger.db replay --last
retie --ledger retie-ledger.db replay --markdown > session.md
```

```
The add function subtracts. Fix it and prove it works.
ses_e56066785aa6 · gpt-oss:20b · retie-default@68219a513562 · bwrap (no network)

  09:34:48  ok        read     read calc.py
  09:34:48  ok        edit     edit calc.py
  09:34:48  ok        write    write config.py
              ⚑ high: hardcoded-secret
  09:34:48  ok        bash     run shell command: python -m pytest

  4 actions · 0 refused · 0 not reversible · 1 flagged · 1 turns · 1s
```

Manus ships shareable replays and it's a good feature. The difference here is that the chain gets verified before anything is printed, so the replay is checkable rather than just plausible.

Refusals render as prominently as successes. A refused call never becomes an action, so it's attributed to whichever session was open rather than dropped. A replay that only showed successes would be the exact misleading artefact the ledger exists to prevent.

## Evidence

The ledger is hash-chained and verified from a separate process:

```bash
retie verify --ledger retie-ledger.db
```

A log you can only check from inside the process that wrote it isn't evidence.

The chain also opens with what the agent was asked: your objective in your own words, the model that served it, and digests of the policy and system prompt in force. Without that the record tells you what happened but not why, and "why did it delete that?" is the first thing anyone asks. The EU AI Act's Article 12, in force for high-risk systems since 2 August 2026, names inputs for the same reason.

## Built on

[revoco](https://github.com/rsh1k/revoco) provides the control plane: reversibility classification, the consequence-aware gate, the reversal engine, the delegation chain, and the ledger. This repo is the terminal agent around it, so the tool surface, the loop, and the judgement about what each tool costs to undo.

One thing worth saying plainly. revoco's `PRA02` detector caught the first version of this code claiming `write` was reversible when I hadn't actually wired the undo up yet, and it blocked the action instead of believing the claim. That's the behaviour the whole design rests on, and I found it by running the thing rather than by reading it.

## What this doesn't do

- **It doesn't stop prompt injection.** Nothing does. It limits what a successful injection can reach.
- **The sandbox is bubblewrap, not a VM.** Shell commands get their own namespaces, no network, a tmpfs `$HOME`, and a read-only allowlist of system paths. `/home`, `/root`, `/mnt` and `/media` are simply absent, so `~/.ssh/id_rsa` and other projects' `.env` files can't be reached. That last part matters more than it looks: without it, a confined command can read a secret, write it into the workspace, and the agent then hands it to the model. Cutting the network doesn't close that path, because the exfiltration route is the agent.
- **No seccomp filter yet.** `Seccomp: 0` inside the sandbox. Namespaces and mounts are enforced, syscalls aren't filtered. Next hardening step.
- **File tools aren't sandboxed**, just path-confined in-process. They can't execute anything so the exposure is different in kind, but it isn't the same guarantee.
- **No supply-chain or rogue-agent coverage** (OWASP ASI04, ASI10). Out of scope for now rather than half-done.
- **Inspection is regex over a single file and knows nothing about your architecture.** Commercial tools in this space back the same idea with a graph of the customer's codebase and a risk model built from many organisations' findings. That's data, not code, and I'm not going to pretend a few hundred lines of pattern matching is the same thing. What's here is narrower: specific, checkable things an agent does to the code in front of it. It'll miss anything needing cross-file reasoning, and it has no measured false-positive rate, only the negative cases pinned in `test_loop.py`.
- **`--network` re-opens exfiltration.** You need it for installs, but while it's on, anything the command reads can leave, and no filesystem snapshot undoes that. The CLI warns at startup.
- **Approval fatigue is real.** If every `bash` prompt gets a reflexive yes then the gate is decoration. `--yes` exists for scripted runs and prints a warning, because a control that's always bypassed should admit it.
- **Keys are per-session.** Human and agent keypairs are generated at startup, so the delegation chain proves integrity within a session but doesn't carry identity across them yet.

## Status

On PyPI as [`retie`](https://pypi.org/project/retie/). Releases are automatic: push to `main` and a patch version publishes via Trusted Publishing, no API token anywhere. See [docs/RELEASING.md](docs/RELEASING.md); there's one manual PyPI form to fill in before the first release works.

- **Ollama Cloud path is verified live.** A full session on `gpt-oss:20b` read a file, edited it, and ran a shell command to check its own work. Streaming, tool-call reassembly, the gate, the sandbox, the ledger and usage accounting all held up.
- **Anthropic path** is tested through the fake backend only, not against the live API.
- The safety plane, sandbox, undo and ledger all run without a key: `python demo.py`.

Three things the live run turned up that mocks couldn't:

1. **`gpt-oss` streams a separate `reasoning` field** alongside `content`. I keep it out of the assistant text, since feeding a model's own thinking-aloud back as dialogue teaches it that scratchpad is conversation, and surface it dimmed instead. With a small `max_tokens` the reasoning eats the whole budget and `content` comes back empty.
2. **Usage is missing from the stream unless you ask for it.** You need `stream_options: {"include_usage": true}`. Without it my local accounting silently sat at zero.
3. **Cheapest-first isn't obviously right.** `gpt-oss:20b` fixed a one-line bug in 7 tool calls, three of which were globs and an `ls -R` looking for a file it had already been given the name of. Seven tier-1 calls can cost more than two tier-3 calls. The ladder still starts cheap, but that's a stated assumption now rather than a proven one, and it's the next thing I want to measure.

One last thing that sold me on keeping the ledger. The model claimed it had "verified with a test call that outputs 5". The ledger showed it really had run `python -c 'import calc…'`. Being able to check a model's account of its own work against an independent record turns out to be worth the trouble.
