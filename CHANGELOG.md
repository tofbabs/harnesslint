# Changelog

## 0.3.0 — 2026-09-03

A seventh dimension: `placement`. The other six each ask a question about one artifact
in isolation; `placement` asks the question that comes first in practice — is this
piece of guidance living in the primitive whose job it is (project instructions file,
scoped rules, skills, subagents, hooks)? Misplacement is the same fail-open shape the
tool was extracted to catch, one level up: a procedure in `CLAUDE.md` paid for on every
turn and read closely on none, a skill that only applies when someone thinks to invoke
it, a scoped file over a directory that no longer holds anything, a hook registered on a
misspelled event that never fires and never says so.

**Ten new rules**, hard-mechanical only — no prose matching, nothing that requires
reading intent:

- `placement/no-instruction-file` (error) — harness artifacts exist but no root-level
  instruction file.
- `placement/procedure-in-memory` (warn) — a root memory doc has a run of
  `max_memory_steps` or more consecutive ordered-list items, counted outside fenced
  code. `budget/memory-file` already owns file size; this does not restate it.
- `placement/scope-matches-nothing` (error) — a scoped instruction file whose directory
  contains nothing but instruction files.
- `placement/scope-not-declared` (warn) — the profile declares a `scope_key` and the
  doc's frontmatter lacks it. Inert under every profile shipped today.
- `placement/skill-without-steps` (warn) — a skill body with zero ordered-list items and
  no `## Step` headings. The mirror of `procedure-in-memory`.
- `placement/agent-tool-unknown` (error) — a name in an agent's `tools:` the profile
  does not know, is not an MCP name, and is not in `placement.extra_tools`.
- `placement/agent-body-oversized` (warn) — agent body over `max_agent_tokens`
  estimated tokens. `authority/agent-inherits-all` already owns the bounded-exploration
  half of the subagent definition; this does not restate it.
- `placement/hook-unknown-event` (error) — a key under the settings hooks root that is
  not one of the profile's known hook events.
- `placement/no-hooks-registered` (info) — a settings file parses, the profile knows
  about hooks, and zero are registered. A fact, not a failure.
- `placement/primitives-unavailable` (info) — names the primitives the active profile
  has no concept of, the same discipline as `executability/shell-syntax-unchecked` and
  `executability/exec-bit-unverifiable`: a short placement report under a thin profile
  must not read as a clean one. `placement` is not in `inert_dimensions` for any
  shipping profile — the memory rules apply regardless of profile, so the dimension is
  always at least partially measured.

Three rules that would need prose matching or reading intent were considered and not
shipped: an obligation stated in prose that no hook enforces, a subagent with no return
contract, and a rule in the root instructions file that ought to be scoped. See the
README's design notes for why each fails the hard-mechanical bar.

**New `Profile` fields**, all empty by default so every existing profile stays valid:
`known_tools`, `hook_events`, `scope_key`. All three can be overridden per-project under
`[runner]`, the same as `profile` itself.

**New `[placement]` config block**: `max_memory_steps` (default 6), `max_agent_tokens`
(default 1500), `extra_tools` (default `[]`, for plugin-provided tools a profile's
`known_tools` cannot know about), and `instruction_file_names` (what counts as an
instruction file, not content, for `scope-matches-nothing`).

**What newly gets measured.** The skills glob widens from `.claude/skills/*/SKILL.md`
to `.claude/skills/**/SKILL.md`. A nested skill previously received zero checks and
reported clean — the exact failure mode this tool exists to name. Existing repos with
nested skills will see new findings on this upgrade; that is the ratchet working, not a
regression to chase down by hand — baseline it like any other newly-measured surface.

**`init --scaffold`.** `init` now always writes `.harnesslint/PRIMITIVES.md`, describing
the five primitives and which placement rule speaks to each. A new `--scaffold` flag
additionally writes, only where absent: a root instructions stub, an example skill with
numbered steps, a nested scoped-instructions example, and a settings hook stub.
Opt-in — writing into someone's `.claude/` uninvited is not a linter's business — and
`init` still never overwrites.

`RULESET_VERSION` stays at **2**. Every placement rule is new, and adding rules does not
bump it; the skills-glob widening changes coverage, not the meaning of any existing rule
id.

## 0.2.0 — 2026-09-02

Agnostic, deterministic, installable. No new dimensions; the six are unchanged.

**Runner profiles.** Everything specific to one agent runner moved out of the checks and
into a `Profile`: file layout, tool names, the settings schema, frontmatter keys, the
argument sigil. `claude-code`, `agents-md` and `generic` ship, the profile is detected
from the tree, and `harnesslint profiles` shows which one you get and which dimensions
it cannot speak to. Adding a runner is now a data change.

A Claude Code repo measures exactly what it measured in 0.1.0 — `tests/test_profiles.py`
holds the finding set as a regression lock rather than asserting it in prose.

**The determinism contract**, now stated and tested: same tree + same profile + same
config + same ruleset version → identical bytes, on any machine, any supported Python,
any hash seed. Three leaks are closed, all of which survived a suite that ran the tool
twice in one directory:

- `executability/hook-syntax` embedded `bash -n`'s stderr, which was invoked with an
  absolute path — so the finding message, and therefore its fingerprint, carried the
  checkout location. A baseline recorded on a laptop could not match CI.
- The same rule embedded `ast.parse`'s `SyntaxError` text. That wording is an
  interpreter implementation detail — CPython has reworded these messages across
  releases before — so the fingerprint was hostage to which Python ran the check. The
  messages happen to agree across 3.12–3.14 today; relying on that is not a contract.
- Token estimates counted CRLF as two characters, so a Windows checkout read as a budget
  regression against a baseline recorded on Linux.

**Path-dependence is now declared rather than assumed**, the same principle
`declared_clis` already applied to harnesses:

- `executability.shell_syntax_check` defaults to **off**. A verdict that depends on
  whether bash is installed is not reproducible. When off, `executability/shell-syntax-unchecked`
  reports that shell hooks went unchecked instead of leaving the gap silent.
- `executability.exec_bit_check` defaults to `"auto"`, skipping the executable-bit rule
  where the filesystem cannot carry one (Windows, `core.fileMode=false`, zip exports)
  and emitting `executability/exec-bit-unverifiable` instead of flagging every hook.

**New:** `completeness/settings-unparseable`. A malformed settings file used to be
swallowed, which switched off the allowlist and both hook checks and reported clean —
the tool doing the exact thing it exists to catch.

**Packaging.** `__version__` in the module is now the single source of truth (a vendored
copy carries its own version), PEP 639 licence metadata, Python 3.14 tested and
classified, and a tag-triggered release workflow using PyPI Trusted Publishing. CI now
installs the built wheel and exercises the console script, which nothing tested before.

**Breaking:** `RULESET_VERSION` is now `2`, so existing baselines are rejected with a
warning — re-run `harnesslint baseline` after reviewing the diff. `redundancy.max_duplicates`
was declared but never read, and is removed.

## 0.1.0 — 2026-09-01

First release. Extracted from the harness audit of a live trading-agent repo,
where the six dimensions were derived from defects that had actually shipped.

- Six dimensions: `budget`, `redundancy`, `references`, `completeness`,
  `authority`, `executability`.
- Baseline ratchet — CI fails on regression, not on absolute cleanliness, so a
  messy harness can adopt this without a flag day.
- Project rules via `.harnesslint/rules/*.py`.
- `init`, `report`, `baseline`, `check`, `explain`; `text` / `json` / `github`
  output formats.
- Zero runtime dependencies; runs vendored as a single file or pip-installed.

Known scope limits, stated rather than discovered:

- `paths` defaults assume Claude Code's layout. Other runners work, but need
  their globs configured. (Addressed in 0.2.0 by runner profiles.)
- Token counts are `chars / 4`, not a real tokenizer — a ratchet, not a bill.
- Shell parsing in `executability` is a line scanner with quote and heredoc
  tracking, not a shell grammar. It errs toward silence when it desyncs.
