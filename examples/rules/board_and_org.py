"""
A REAL project rule file, kept verbatim from the repo harnesslint was
extracted from. Copy it as a shape, not as content — every assertion here
is specific to that project.

The seven built-in dimensions are what generalises across harnesses. These are
this repo's own conventions, each one recording a defect that actually shipped:

  * A hardcoded GitHub Projects option id. Board #6's Status field was
    re-created on 2026-07-12 and every option id changed. A stale id is a QUIET
    failure — `gh` exits 0, the card silently stays in (no status), which
    experiments/README.md defines as "not queued for work". Every epic filed by
    /experiment:new between the wipe and the fix was invisible to
    /experiment:start.

  * The pre-move org slug. The repo moved to `kodobe`; GitHub's transfer
    redirect masks a stale `tofbabs/...` reference until it doesn't. The board
    is still owned by the *user* `tofbabs`, so `--owner tofbabs` is correct and
    must NOT be "fixed" — this file guards both directions.

  * A board move with no fallback. Projects v2 is GraphQL-only, so there is no
    MCP equivalent and a cloud session cannot do it at all. Skipping it silently
    leaves a card in the wrong column, which is invisible to the rest of the
    lifecycle — the same quiet failure a stale option id causes.

Contract: expose `check(harness) -> list[Finding]`. The harness object carries
`.root`, `.config`, `.memory`, `.commands`, `.agents`, `.skills`, `.settings`.
"""

from __future__ import annotations

import re

from harnesslint import ERROR, WARN, Finding, Harness

STALE_SLUG = "tofbabs/polymarket-trading-agent"
BOARD_OWNER = "tofbabs"

# `gh project item-edit --single-select-option-id 4100634a`
HARDCODED_OPTION_ID = re.compile(r"--single-select-option-id\s+[0-9a-f]{6,}")
# The runtime read that replaces it.
RUNTIME_READ = "ProjectV2SingleSelectField"
BOARD_MUTATION = "gh project item-edit"


def check(h: Harness) -> list[Finding]:
    out: list[Finding] = []

    for doc in h.all_docs:
        text = doc.raw

        for n, line in enumerate(text.splitlines(), 1):
            if HARDCODED_OPTION_ID.search(line):
                out.append(
                    Finding(
                        "project",
                        "project/hardcoded-board-id",
                        ERROR,
                        doc.rel,
                        "hardcodes a board Status option id; read it at runtime via the "
                        f"`{RUNTIME_READ}` GraphQL query — a stale id makes `gh` exit 0 "
                        "while the card silently stays in (no status)",
                        n,
                    )
                )
            if STALE_SLUG in line:
                out.append(
                    Finding(
                        "project",
                        "project/stale-org-slug",
                        ERROR,
                        doc.rel,
                        f"targets `{STALE_SLUG}`; the repo moved to the `kodobe` org and "
                        "GitHub's transfer redirect hides this until it stops working",
                        n,
                    )
                )
            # The inverse mistake: a blanket tofbabs->kodobe rewrite breaks
            # every board call, because board #6 belongs to the user.
            if (
                "gh project item-" in line
                and "--owner" in line
                and f"--owner {BOARD_OWNER}" not in line
            ):
                out.append(
                    Finding(
                        "project",
                        "project/wrong-board-owner",
                        ERROR,
                        doc.rel,
                        f"board #6 is owned by the user `{BOARD_OWNER}`, not the org — "
                        "the repo moved but the board did not",
                        n,
                    )
                )

        if BOARD_MUTATION in text:
            if RUNTIME_READ not in text:
                out.append(
                    Finding(
                        "project",
                        "project/board-move-without-runtime-read",
                        ERROR,
                        doc.rel,
                        "mutates the board but never reads the option ids at runtime",
                    )
                )
            if "OPERATOR TODO" not in text:
                out.append(
                    Finding(
                        "project",
                        "project/board-move-no-fallback",
                        WARN,  # raised to error in harnesslint.toml
                        doc.rel,
                        "moves a board card, which is impossible without `gh` (Projects v2 "
                        "is GraphQL-only and has no MCP equivalent). It must degrade to a "
                        "reported operator TODO, never a silent skip",
                    )
                )

    return out
