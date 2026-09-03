# Examples

Illustrative only. Nothing in this directory is loaded when `harnesslint` runs — these
are files to copy out, not files that do anything where they sit.

| File | Copy it to | What it is |
| :-- | :-- | :-- |
| `harnesslint.toml` | `<repo root>/harnesslint.toml` | Every supported config key, annotated with why you would set it. Delete everything you do not need — the defaults are the opinions. |
| `rules/board_and_org.py` | `<repo root>/.harnesslint/rules/` | A real project rule file, kept verbatim from the repo this tool was extracted from. |

`harnesslint init` scaffolds a shorter version of both, and does not overwrite.

## About the project rule

`rules/board_and_org.py` is deliberately not generic. It hardcodes one team's GitHub org
slug, one project-board number and one board-owner login, because that is what a project
rule *is*: the assertions only your repo can make, which have no business being upstream.

**Copy it as a shape, not as content.** What generalises is the pattern — a magic id
that must never be pasted into a command, a service that must always be pinned, a
convention your team agreed on and keeps half-forgetting. The seven built-in dimensions
cover what every harness has in common; this is where everything else goes.

The contract is one function:

```python
from harnesslint import ERROR, Finding, Harness

def check(h: Harness) -> list[Finding]:
    ...
```

The harness object carries `.root`, `.config`, `.profile`, `.memory`, `.commands`,
`.agents`, `.skills`, `.settings` and `.all_docs`. A rule that raises is reported as
`project/rule-error` and does not mask the others.

Project rules are arbitrary Python, so they sit **outside** the tool's determinism
contract. If yours reads the clock, the network or the environment, its findings will
churn the baseline and the ratchet stops being trustworthy. Keep them pure functions of
the tree.
