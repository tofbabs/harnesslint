# harnesslint

Measure drift in an agent harness, deterministically, and fail CI when it gets worse.

An **agent harness** is the configuration that steers a coding agent — the instruction
file (`CLAUDE.md`, `AGENTS.md`), slash commands, sub-agents, skills, hooks, tool grants.
It is executable configuration that nothing validates. It rots the way documentation
rots: silently, and in the direction of looking fine.

```
$ harnesslint check
harnesslint — 7 dimensions, ruleset v2, profile claude-code

  budget         clean                        OK
  redundancy     14 finding(s)                WARN
  references     clean                        OK
  completeness   clean                        OK
  authority      1 finding(s)                 FAIL
  executability  clean                        OK
  placement      clean                        OK

Findings
  NEW error authority/read-only-contradiction
        .claude/agents/auditor.md: describes itself as read-only but holds Write —
        the prose and the grant disagree, and only one is enforced

Regression vs baseline: authority 0 -> 1
```

## Why

This was extracted from the audit of one real harness, where four defects had
accumulated unnoticed. Every one shared a shape, and it is the shape the whole tool
is aimed at: **each failed open and reported success.**

| Defect | What it looked like | What was true |
| :-- | :-- | :-- |
| Stale project-board id | CLI exits 0 | Card silently went nowhere |
| Hooks never installed | Commits succeeded | No lint gate, no test gate, nothing ran |
| Stale org slug | Everything worked | Masked by a redirect, until it wasn't |
| Commands built on an absent CLI | Read fine | Failed at step 1 in half the environments |

None was visible without reading every harness file at once. That is a job for a
checker, and it is the one thing a checker is reliably better at than a person.

## Install

```bash
pip install harnesslint
```

Or vendor it — it is one stdlib-only file, and this path is fully supported:

```bash
curl -O https://raw.githubusercontent.com/kodobe/harnesslint/main/harnesslint.py
python3 harnesslint.py report
```

Python 3.11+ (for `tomllib`). **No runtime dependencies, on purpose**: this tool
exists to be trustworthy when the project it measures is broken — including when
that project's dependencies are what is broken.

## Use

```bash
harnesslint init            # scaffold a config, a rules dir, and a CI snippet
harnesslint report          # every finding, never fails the build
harnesslint baseline        # accept the current state as the ratchet
harnesslint check           # compare to the baseline — this is what CI runs
harnesslint check --strict  # ignore the baseline, fail on any error
harnesslint explain budget  # what a dimension means and why
harnesslint profiles        # which runners are known, and what each covers
```

Exit codes: `0` no regression, `1` regression (or any error under `--strict`),
`2` a usage problem.

### In CI

```yaml
  harness:
    name: harness gate
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with: { python-version: "3.11" }
      - run: pipx run harnesslint check --format github
```

`--format github` emits `::error` / `::warning` annotations, so a finding is clickable
on the PR diff rather than buried in log output. `--format json` is there for anything
else you want to build on top.

Keep this job **separate** from your test job. One gates the configuration that steers
the agent, the other the code it writes; they fail for unrelated reasons, and a harness
regression should name itself instead of hiding inside a test run.

## The seven dimensions

| Dimension | The question it asks |
| :-- | :-- |
| **budget** | What does this harness cost on *every* turn, before any work? |
| **redundancy** | Is any rule stated twice, where one copy can drift? |
| **references** | Does every path and link the harness cites actually exist? |
| **completeness** | Is every artifact fully declared, or only partly configured? |
| **authority** | Does anything hold more power than its own description claims? |
| **executability** | Can the commands run in the environment they target? |
| **placement** | Is this piece of guidance living in the primitive whose job it is? |

Two are worth expanding, because they are the ones people misread.

**budget** separates two populations, and conflating them is the usual mistake. Root
memory and every artifact's `description:` load on *every* turn — a command's
description is paid for whether or not it is ever invoked. A command's **body** is not:
it loads on invocation. So a long, careful procedure is cheap and a long description is
not, and the lever for cutting always-loaded cost is a scoped instruction file (loaded
on contact), never deleting a rule.

Token counts are `chars / 4`, a deliberate approximation. A real tokenizer means a
dependency and a model choice, for a number whose job is to be compared against its own
previous value. The divisor is configurable if your content tokenizes unusually.

**executability** does not object to depending on a CLI. It objects to depending on one
*silently*. Declaring `gh` or `kubectl` in your config turns "this breaks in CI for
reasons nobody understands" into a stated precondition every command can handle.

## The five primitives

The other six dimensions each ask a question about one artifact in isolation.
**placement** asks the question that comes first in practice: is this piece of guidance
living in the primitive whose job it is?

| Primitive | Its job |
| :-- | :-- |
| Project instructions file | The handful of facts true for every task. Build command, test command, house conventions that are not enforceable, the one architectural rule everyone gets wrong. Short. |
| Scoped rules | Conventions that apply to part of the tree, loaded on contact. This is how the always-loaded file stays small without losing the guidance. |
| Skills | A procedure with steps, invoked when a kind of task comes up. |
| Subagents | A bounded task whose exploration is large and whose answer is small. The isolation is the point. |
| Hooks | Anything that must happen or must not happen. |

Misplacement is the same fail-open shape the tool was extracted to catch. A procedure in
`CLAUDE.md` is paid for on every turn and read closely on none. A skill that is really a
rule only applies when someone thinks to invoke it. A scoped file over a directory that
no longer holds anything is a rule that silently stopped applying. A hook registered on a
misspelled event never fires and reports nothing.

**placement** is deliberately narrower than the planned `coherence` dimension (see
[ROADMAP.md](ROADMAP.md)), which is about instructions contradicting each other:
placement asks only whether each piece of guidance is in the right *kind* of file. Hard
mechanical checks only — no prose matching, nothing that requires reading intent.

| Rule | Severity | What trips it |
| :-- | :-- | :-- |
| **Project instructions file** | | |
| `placement/no-instruction-file` | ERROR | Harness artifacts exist (any command, agent, skill or settings file discovered) but no root-level instruction file. Everything else is steering a runner with no baseline instructions. |
| `placement/procedure-in-memory` | WARN | A root memory doc contains a run of `max_memory_steps` (default 6) or more consecutive ordered-list items, counted outside fenced code. A procedure belongs in a skill, where it is paid for on invocation instead of on every turn. |
| **Scoped rules** | | |
| `placement/scope-matches-nothing` | ERROR | A scoped (non-root) instruction file whose directory holds nothing but instruction files. The rule governs nothing — it is dead, and dead quietly. A subdirectory counts as governed, since scope is a subtree. |
| `placement/scope-not-declared` | WARN | The profile declares a `scope_key` and the doc's frontmatter lacks it. Inert for `claude-code`, where scope is directory placement; the field exists so the Cursor / Copilot profiles planned for a later release land as data rather than code. |
| **Skills** | | |
| `placement/skill-without-steps` | WARN | Skill body has zero ordered-list items and no `## Step` headings. A skill that is not a procedure is a rule wearing a skill's clothes, and a rule that only applies when invoked is a rule that mostly does not apply — the exact mirror of `procedure-in-memory`. |
| **Subagents** | | |
| `placement/agent-tool-unknown` | ERROR | A name in an agent's `tools:` that the profile does not know, is not an MCP name (contains `__`), and is not listed in `placement.extra_tools`. `tools: Wrte` is accepted silently by the runner today — the agent then holds nothing it thinks it holds, and reports success. |
| `placement/agent-body-oversized` | WARN | Agent body over `max_agent_tokens` (default 1500) estimated tokens. At that length it is a procedure being run as a task, which is a skill. |
| **Hooks** | | |
| `placement/hook-unknown-event` | ERROR | A key under the settings hooks root that is not one of the profile's known hook events. A hook on a misspelled event never fires and never says so. |
| `placement/no-hooks-registered` | INFO | A settings file exists and parses, the profile knows about hooks, and zero hooks are registered. Every rule in this harness is advisory; nothing is enforced. A fact, not a failure — hence INFO. |
| **Coverage honesty** | | |
| `placement/primitives-unavailable` | INFO | Names the primitives the active profile has no concept of (e.g. under `agents-md`: skills, subagents, hooks). Same discipline as `executability/shell-syntax-unchecked` and `executability/exec-bit-unverifiable`: a short placement report under a thin profile must not read as a clean one. |

Two things placement deliberately does **not** restate: `budget/memory-file` already
owns memory-file size, and the *bounded* half of the subagent definition is already
covered by `authority/agent-inherits-all`. `placement/procedure-in-memory` and
`placement/agent-body-oversized` are about *shape* — a procedure or a task run in the
wrong primitive — not about size on its own.

`placement` is not listed under `inert_dimensions` for any shipping profile: the memory
rules (`no-instruction-file`, `procedure-in-memory`) apply regardless of what a profile
knows about skills, subagents or hooks, so the dimension is always at least partially
measured. `placement/primitives-unavailable` is what states, every run, exactly how
partial that is under the active profile.

## The ratchet

`check` compares against `.harnesslint-baseline.json` and fails on **regression**, not
on absolute cleanliness. An existing mess is grandfathered; adding to it is not.

That is what lets this be a required check from day one rather than a flag day, and it
is the only mode in which a gate like this survives contact with a real backlog.

```bash
harnesslint report     # read what you are about to accept
harnesslint baseline   # commit the result
```

Findings are fingerprinted on `rule + path + the non-variable part of the message`,
deliberately excluding line numbers and counts — reflowing a paragraph must not read as
a new finding, or the baseline churns and stops being trusted.

**Baselining is not ignoring.** A baselined finding still prints in `report`, so it
stays visible and countable. An `ignore` entry never prints at all. Prefer the baseline:
you almost always want to know if a category grows.

## Runner profiles

The seven dimensions are runner-agnostic. Everything that is *not* — where files live,
what the tools are called, how the settings file is shaped, which sigil means
"arguments" — lives in a **profile**, and the profile is detected from your tree.

```bash
$ harnesslint profiles
 * claude-code
     Claude Code — .claude/ commands, agents, skills, settings and hooks.
     detected by: .claude
     not measured: nothing — all seven apply

   agents-md
     The AGENTS.md convention — an instruction file, and no runner-owned layout.
     detected by: AGENTS.md
     not measured: completeness, authority, executability
```

That last line is the part that matters. A profile with no concept of sub-agents cannot
say anything about `authority`, and a report that quietly printed `clean` for it would
be the exact failure this tool exists to catch — checking nothing, and reading as fine.
So an unmeasured dimension prints `not measured (profile)`, never `OK`.

Detection is a pure function of the tree, so it costs nothing in determinism. Pin it
once the answer matters:

```toml
[runner]
profile = "claude-code"      # or "agents-md", "generic"
```

The profile is also recorded in the baseline. If it changes, `check` refuses the
baseline rather than silently comparing against counts from a different set of files.

Adding a runner is a data change — one entry in `PROFILES`, no new checks.

## Configuration

Everything is optional — the defaults are the opinions, and `harnesslint report` works
on a repo with no config and no harness at all. Config is layered: built-in defaults,
then the profile, then your `harnesslint.toml`. Override only what your project decided
differently:

```toml
[runner]
profile = "agents-md"                    # default: detected from the tree

[paths]
commands = [".claude/commands/**/*.md", "tools/commands/**/*.md"]

[budget]
always_loaded_tokens = 16000

[executability]
declared_clis = ["kubectl", "helm"]      # depended on, and said out loud
shell_syntax_check = true                # opt in where bash is guaranteed

[placement]
max_agent_tokens = 2000                  # this repo's agents run longer tasks

[severities]
"redundancy/duplicate-rule" = "info"     # if your commands genuinely must repeat
```

Three `Profile` fields back the `placement` dimension and default empty, so an existing
profile stays valid without changes: `known_tools` (the runner's tool names — empty
disables `placement/agent-tool-unknown` rather than firing it blindly against a runner
this tool does not know), `hook_events` (the event names a hook may register under), and
`scope_key` (the frontmatter key carrying a scoped rule's glob, for runners where scope
is not directory placement). All three can be overridden under `[runner]` alongside
`profile`, the same as any other profile-supplied value.

The `[placement]` block itself: `max_memory_steps` (default 6) — the ordered-list run
length that makes a root memory doc read as a procedure rather than a fact sheet;
`max_agent_tokens` (default 1500) — the estimated body size past which a subagent reads
as a task-shaped skill; `extra_tools` (default `[]`) — the escape hatch for tool names a
plugin provides that the profile's `known_tools` cannot know about; and
`instruction_file_names` — what counts as "an instruction file, not content" when
`placement/scope-matches-nothing` decides whether a scoped directory governs anything.

## Project rules

The seven dimensions are what generalises. Everything a *specific* repo knows goes in
`.harnesslint/rules/*.py`, each exposing `check(harness) -> list[Finding]`:

```python
from harnesslint import ERROR, Finding, Harness

def check(h: Harness) -> list[Finding]:
    return [
        Finding("project", "project/no-prod-url", ERROR, doc.rel,
                "hardcodes the prod URL; read it from config")
        for doc in h.all_docs
        if "https://api.prod." in doc.raw
    ]
```

The harness object carries `.root`, `.config`, `.memory`, `.commands`, `.agents`,
`.skills`, `.settings` and `.all_docs`. A rule that raises is reported as
`project/rule-error` and does not mask the others.

`examples/rules/board_and_org.py` is a real one, kept verbatim from the repo this was
extracted from — copy it as a shape, not as content.

## Design notes

**Determinism is the whole point**, and it is a contract rather than an aspiration:

> same tree + same profile + same config + same ruleset version → **identical bytes**,
> on any machine, any supported Python, any hash seed.

No network, no clock, no PATH-dependence in the verdict, no LLM. Nothing that varies
between a laptop and CI may reach a finding message, because the message feeds the
fingerprint and the fingerprint backs the ratchet. A fingerprint that moves between
machines makes every run read as drift, which is worse than having no ratchet at all.

Two consequences you can see from the outside. `bash -n` on shell hooks is **off by
default** — a verdict that depends on whether bash happens to be installed is not
reproducible — and when it is off, `harnesslint` says so rather than reporting a clean
result it did not earn. The same applies to the executable-bit check on a filesystem
that cannot carry one. Turn the shell check on where bash is guaranteed:

```toml
[executability]
shell_syntax_check = true
```

**Project rules are outside the contract.** They are arbitrary Python; what they emit is
your responsibility.

**It is a linter, not a reviewer.** It measures what is mechanically checkable and stays
quiet about everything else. A checker that guesses is one people learn to ignore.

**`placement` shipped with three rules deliberately left out**, each for the same
reason: the hard-mechanical bar.

- *An obligation stated in prose that no hook enforces* — "always run the formatter
  before committing," written down and never wired up. Detecting the obligation means
  matching prose. `placement/no-hooks-registered` covers the honest, countable half
  instead: it does not know whether any *specific* obligation is enforced, only that
  this harness enforces nothing at all.
- *A subagent with no return contract* — the "small answer" half of the subagent
  definition, alongside the "bounded exploration" half that `authority/agent-inherits-all`
  already covers. There is no structural signal for a return contract; a body can look
  like anything and still hand back a paragraph or a file tree.
- *A rule in the root instructions file that ought to be scoped* — the mirror image of
  `procedure-in-memory`. Deciding that a rule is scoped, not universal, means deciding
  what the rule is *about*, and that is reading intent, not counting structure.

**Quiet is a feature, and it was expensive.** The first draft of the CLI check reported
`for`, `done`, `import` and `print` as undeclared binaries — 49 findings, nearly all
noise. It now skips heredoc bodies, multi-line quoted strings and non-shell fences, and
resolves `VAR=$(cmd …)` to `cmd`. Most of the test suite is false-positive guards, and
if you add a check, test it against a clean tree **and** an injected defect.

`RULESET_VERSION` is bumped when an existing rule's *meaning* changes; `check` refuses
to trust a baseline recorded under a different one. Adding a new rule does not bump it —
new rules land as findings, which the ratchet already handles.

## Status

`0.3.0`. The dimensions, the ratchet and the determinism contract are settled;
individual rules will move as they meet more real harnesses, and profiles will be added
as they meet more runners. Pin the version if you need the finding set to hold still.

See [ROADMAP.md](ROADMAP.md) for where this goes next, and for what it deliberately
will not do.

## License

MIT — see [LICENSE](LICENSE).
