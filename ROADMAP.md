# Roadmap

## What this tool is

Birgitta Böckeler's [*Harness engineering for coding agent
users*](https://martinfowler.com/articles/harness-engineering.html) gives the vocabulary
this roadmap is built on. Two axes matter here:

- **Guides** (feedforward) steer the agent *before* it acts — the instruction file,
  skills, conventions. **Sensors** (feedback) observe *after* it acts and let it
  self-correct — tests, linters, hooks.
- **Computational** controls are "deterministic and fast… results are reliable."
  **Inferential** ones use a model: "slower and more expensive; results are more
  non-deterministic."

That places `harnesslint` precisely: it is a **computational sensor pointed at the
harness itself**. Everything else in a project's harness regulates the code; this
regulates the thing doing the regulating. Böckeler names the gap it aims at directly:

> Feedforward and feedback controls are currently scattered across delivery steps,
> there's real potential for tooling that helps configure, sync, and reason about them
> as a system.

Getting from "seven checks on some markdown" to that is what the versions below are for.

## Released

### 0.1 — the six dimensions

Extracted from the audit of one real harness. `budget`, `redundancy`, `references`,
`completeness`, `authority`, `executability`, plus the baseline ratchet that makes the
gate adoptable on day one.

### 0.2 — agnostic, deterministic, installable

- **Runner profiles.** Everything specific to one agent runner — file layout, tool
  names, settings schema, frontmatter keys — moved out of the checks and into a
  `Profile`. `claude-code`, `agents-md` and `generic` ship. Adding a runner is now a
  data change.
- **The determinism contract**, stated and tested: same tree + same profile + same
  config + same ruleset version → identical bytes, on any machine, any supported
  Python, any hash seed. Three leaks are closed; see the CHANGELOG.
- **PyPI**, published from a tag via Trusted Publishing.

### 0.3 — placement

A seventh dimension, `placement`, asking the question the other six do not: is each
piece of guidance living in the primitive whose job it is — project instructions file,
scoped rules, skills, subagents, hooks? The same fail-open shape the tool was extracted
to catch, one level up: a procedure in `CLAUDE.md` paid for on every turn and read
closely on none, a skill that is really a rule and only applies when invoked, a scoped
file over a directory that no longer holds anything, a hook registered on a misspelled
event that never fires and never says so.

- Ten rules across the five primitives, `placement/no-instruction-file` through
  `placement/primitives-unavailable` — see the README for the full list.
- Three new empty-by-default `Profile` fields (`known_tools`, `hook_events`,
  `scope_key`) so the dimension is data-driven per runner, the same discipline 0.2
  applied to the other six.
- Hard-mechanical only: no prose matching, nothing that requires reading intent. Three
  rules that would need that — an unenforced prose obligation, a subagent with no
  return contract, a root-file rule that ought to be scoped — were scoped out rather
  than shipped as guesses. See the README's design notes.
- `.claude/skills/**/SKILL.md` replaces `.claude/skills/*/SKILL.md` as the skills glob:
  a nested skill went entirely unchecked before this. This surfaces new findings on
  existing trees, which is what the ratchet is for.
- `init` now writes `.harnesslint/PRIMITIVES.md`, the taxonomy the dimension measures
  against, and gains `--scaffold` to stub the primitives themselves. Opt-in, and it
  still never overwrites.
- `RULESET_VERSION` stays at 2 — every placement rule is new, and new rules land as
  findings rather than changing what an existing rule id asserts.

## Planned

### 0.4 — signals optimised for agent consumption

Böckeler's sharpest practical point is that a sensor is only as good as the signal it
emits: sensors are "particularly powerful when they produce signals that are optimised
for LLM consumption, e.g. custom linter messages that include instructions for the
self-correction." Today `harnesslint` writes for a human reading CI logs.

- `--format agent` — each finding carries what to do about it and a **sanctioned escape
  hatch**. Her threshold pattern is the model to copy: rather than a binary
  suppress-or-comply choice, the agent may raise a budget slightly, "so that the rule
  fires again if it gets even worse in the future. Constraints are preserved."
- `--format sarif`, so findings land in GitHub code scanning rather than log output.
- Per-rule `explain`. Today `explain budget/always-loaded` prints the dimension's
  docstring, so all four `budget/*` rules explain identically.

### 0.5 — coherence

Her open question, and the one a checker is actually well placed to answer:

> How do we keep a harness coherent as it grows, with guides and sensors in sync, not
> contradicting each other?

An eighth dimension, `coherence`: two instructions that contradict each other across
files; a guide that describes a sensor which is not wired up; a hook registered for an
event nothing documents; a command that references a workflow step that no longer
exists. Plus `harnesslint diff`, scoping a report to what a PR changed.

The boundary against 0.3's `placement` is exact, not incidental: **placement asks
whether guidance is in the right kind of file; coherence asks whether two pieces of
guidance agree.** A rule that is correctly placed can still contradict another rule
that is also correctly placed — that is coherence's problem, not placement's.

### 0.6 — the guide/sensor inventory

A harness with guides but no sensors produces "an agent that encodes rules but never
finds out whether they worked." A harness with sensors but no guides produces "an agent
that keeps repeating the same mistakes." Neither is visible from inside one file.

`harnesslint inventory` reads what the repo already has — pre-commit config, CI
workflows, lint and type-checker configs, agent hooks — classifies each control by
direction (feedforward/feedback) and execution (computational/inferential), and reports
the balance. This is the "reason about them as a system" half of the quote above, and
the point at which the tool stops being a markdown linter.

### 0.7 — more profiles, and harness templates

`cursor`, `github-copilot`, `gemini-cli`, `opencode`. `init --profile`. Profiles
contributable as data-only pull requests, with a documented schema, so supporting a
runner does not require understanding the checks.

Beyond that, Böckeler's **harness templates**: "a bundle of guides and sensors that leash
a coding agent to the structure, conventions and tech stack of a topology." A profile
says where a runner keeps its files; a template says what a good harness for *this kind
of service* contains. She also names the trap — templates "start to fall out of sync
with upstream improvements" — so this only ships with an answer to drift.

### 1.0 — the stability contract

Rule ids frozen. A documented deprecation policy. A published profile schema. SemVer
discipline on `RULESET_VERSION`. Until then, pin the version if you need the finding set
to hold still.

## Non-goals

A roadmap is worth more for what it refuses.

- **Anything inferential.** The moment a model judges a harness, the determinism
  guarantee is gone, and with it the ratchet — every run would read as drift. Semantic
  judgment about a harness is a real need and a different tool.
- **A real tokenizer in the default path.** It means a dependency and a model choice for
  a number whose job is to be compared against its own previous value. An optional extra
  at most; `chars/4` is a ratchet, not a bill.
- **Linting the repo's code.** There are excellent tools for that, and Böckeler's
  follow-up, [*Maintainability sensors for coding
  agents*](https://martinfowler.com/articles/sensors-for-coding-agents.html), is the
  better guide to configuring them. This tool checks the harness, not the codebase.
- **Runtime or production sensing.** Out of scope by construction: no network, no clock.
