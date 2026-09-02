# Changelog

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
