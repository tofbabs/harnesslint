"""
tests/test_harnesslint.py — regression for the seven built-in dimensions.

The tool's only job is to be trusted. Two properties carry that, and both are
easy to lose silently:

  * **Determinism.** Same tree in, same bytes out. A checker whose output moves
    between runs cannot back a ratchet, because every run looks like drift.
  * **Quiet.** The first draft of the CLI check reported `for`, `done`, `import`
    and `print` as undeclared binaries — 49 findings, nearly all noise. A
    checker that cries wolf is one people learn to ignore, which is the failure
    it exists to prevent. Most of the tests below are false-positive guards.

Fixtures are built as synthetic harnesses in tmp_path rather than pointed at
this repo, so the tests keep meaning after this repo's own harness changes.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TOOL = _ROOT / "harnesslint.py"

sys.path.insert(0, str(_ROOT))
import harnesslint as hl  # noqa: E402


# ── fixtures ──────────────────────────────────────────────────────────────
def build_harness(
    root: Path,
    *,
    commands=None,
    agents=None,
    memory=None,
    settings=None,
    skills=None,
    no_memory=False,
) -> Path:
    """Write a synthetic harness and return its root.

    `skills` maps a path relative to `.claude/skills/` (e.g. `"demo/SKILL.md"`
    or a nested `"group/nested/SKILL.md"`) to its content. `no_memory` skips
    writing a root `CLAUDE.md` entirely, for fixtures that need to be caught
    without one.
    """
    (root / ".claude" / "commands").mkdir(parents=True, exist_ok=True)
    (root / ".claude" / "agents").mkdir(parents=True, exist_ok=True)
    if not no_memory:
        (root / "CLAUDE.md").write_text(memory or "# Project\n\nA rule.\n")
    for name, text in (commands or {}).items():
        (root / ".claude" / "commands" / name).write_text(text)
    for name, text in (agents or {}).items():
        (root / ".claude" / "agents" / name).write_text(text)
    for rel, text in (skills or {}).items():
        path = root / ".claude" / "skills" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    if settings is not None:
        (root / ".claude" / "settings.json").write_text(json.dumps(settings, indent=2))
    return root


def findings(root: Path) -> list[hl.Finding]:
    return hl.run(hl.discover(root, hl.load_config(root)))


def rules(root: Path) -> set[str]:
    return {f.rule for f in findings(root)}


# ── determinism ───────────────────────────────────────────────────────────
class TestDeterminism:
    """Without this the ratchet is worthless: every run reads as drift."""

    def test_repeated_runs_are_byte_identical(self, tmp_path: Path) -> None:
        build_harness(
            tmp_path,
            commands={
                "a.md": "---\ndescription: A\n---\n\nDo a thing with $ARGUMENTS.\n",
                "b.md": "---\ndescription: B\n---\n\n```bash\ngh issue view 1\n```\n",
            },
        )
        runs = [
            [(f.rule, f.path, f.message) for f in findings(tmp_path)],
            [(f.rule, f.path, f.message) for f in findings(tmp_path)],
        ]
        assert runs[0] == runs[1]

    def test_findings_are_totally_ordered(self, tmp_path: Path) -> None:
        """Sorted output means a diff of two reports is readable."""
        build_harness(
            tmp_path,
            commands={f"c{i}.md": "---\ndescription: C\n---\n\n$ARGUMENTS\n" for i in range(5)},
        )
        got = findings(tmp_path)
        keys = [
            (hl._SEVERITY_ORDER[f.severity], f.dimension, f.rule, f.path, f.message) for f in got
        ]
        assert keys == sorted(keys)

    def test_fingerprint_survives_reflowing(self) -> None:
        """A line move or a renumbered count must not read as a new finding."""
        a = hl.Finding("budget", "budget/x", hl.WARN, "CLAUDE.md", "is ~1200 est. tokens", 10)
        b = hl.Finding("budget", "budget/x", hl.WARN, "CLAUDE.md", "is ~1310 est. tokens", 88)
        assert a.fingerprint == b.fingerprint

    def test_fingerprint_separates_different_files(self) -> None:
        a = hl.Finding("budget", "budget/x", hl.WARN, "a.md", "too big")
        b = hl.Finding("budget", "budget/x", hl.WARN, "b.md", "too big")
        assert a.fingerprint != b.fingerprint


# ── the noise guards ──────────────────────────────────────────────────────
class TestExecutabilityIsQuiet:
    """Every case here produced a false finding in the first draft."""

    def test_shell_keywords_are_not_binaries(self) -> None:
        block = "for f in a b; do\n  echo $f\ndone\nif true; then\n  exit 0\nfi\n"
        assert hl._shell_commands(block) == set()

    def test_heredoc_bodies_are_skipped(self) -> None:
        block = "python3 - <<'PY'\nimport sys\nprint(sys.path)\nPY\n"
        assert hl._shell_commands(block) == {"python3"}

    def test_multiline_double_quoted_strings_are_skipped(self) -> None:
        """`python -c \"` then four lines of Python is ONE command."""
        block = (
            'python -c "\n'
            "import sqlite3, json\n"
            "conn = sqlite3.connect('x.db')\n"
            "result = conn.execute('SELECT 1').fetchone()\n"
            '"\n'
        )
        assert hl._shell_commands(block) == {"python"}

    def test_multiline_single_quoted_strings_are_skipped(self) -> None:
        block = (
            'gh api graphql -f query=\'{ node(id: "X") {\n'
            "  ... on ProjectV2SingleSelectField { options { id name } } } }'\n"
        )
        assert hl._shell_commands(block) == {"gh"}

    def test_command_substitution_resolves_to_the_inner_command(self) -> None:
        """`ITEM=$(gh project …)` is `gh`, not `project`."""
        assert hl._shell_commands("ITEM=$(gh project item-list 6)\n") == {"gh"}

    def test_env_prefix_resolves_to_the_command(self) -> None:
        assert hl._shell_commands("FOO=bar railway status\n") == {"railway"}

    def test_non_shell_fences_are_not_read_as_shell(self, tmp_path: Path) -> None:
        build_harness(
            tmp_path,
            commands={"a.md": "---\ndescription: A\n---\n\n```python\nimport os\nprint(os)\n```\n"},
        )
        assert "executability/undeclared-cli" not in rules(tmp_path)

    def test_a_real_external_cli_is_still_reported(self, tmp_path: Path) -> None:
        """Quiet must not mean silent — the signal has to survive."""
        build_harness(
            tmp_path,
            commands={"a.md": "---\ndescription: A\n---\n\n```bash\nterraform apply\n```\n"},
        )
        assert "executability/undeclared-cli" in rules(tmp_path)


class TestReferences:
    """Three different problems hide behind one unresolved citation."""

    def test_existing_path_is_clean(self, tmp_path: Path) -> None:
        build_harness(tmp_path, memory="# P\n\nSee [t](docs/t.md).\n")
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "t.md").write_text("hi")
        assert not [f for f in findings(tmp_path) if f.dimension == "references"]

    def test_bare_filename_that_exists_elsewhere_is_unqualified_not_broken(
        self, tmp_path: Path
    ) -> None:
        """Saying a file "does not exist" when it does is how a linter loses trust."""
        build_harness(tmp_path, memory="# P\n\nEnforced by `pr-title-check.yml`.\n")
        (tmp_path / ".github" / "workflows").mkdir(parents=True)
        (tmp_path / ".github" / "workflows" / "pr-title-check.yml").write_text("on: push")
        assert "references/unqualified-path" in rules(tmp_path)
        assert "references/missing-path" not in rules(tmp_path)

    def test_ambiguous_basename_is_its_own_finding(self, tmp_path: Path) -> None:
        build_harness(tmp_path, memory="# P\n\nRun `deploy.sh`.\n")
        for d in ("scripts", "deploy"):
            (tmp_path / d).mkdir()
            (tmp_path / d / "deploy.sh").write_text("#!/bin/sh\n")
        assert "references/ambiguous-path" in rules(tmp_path)

    def test_a_genuinely_missing_link_is_an_error(self, tmp_path: Path) -> None:
        build_harness(tmp_path, memory="# P\n\nSee [gone](docs/gone.md).\n")
        got = [f for f in findings(tmp_path) if f.rule == "references/broken-link"]
        assert got and got[0].severity == hl.ERROR

    def test_placeholders_are_not_references(self, tmp_path: Path) -> None:
        build_harness(tmp_path, memory="# P\n\nEdit `tests/unit/test_<module>.py`.\n")
        assert not [f for f in findings(tmp_path) if f.dimension == "references"]


class TestAuthority:
    def test_read_only_claim_with_a_mutating_tool_is_an_error(self, tmp_path: Path) -> None:
        build_harness(
            tmp_path,
            agents={
                "a.md": "---\nname: a\ndescription: Read-only; never merges.\n"
                "tools: Bash, Read, Write\n---\n\nYou report.\n"
            },
        )
        assert "authority/read-only-contradiction" in rules(tmp_path)

    def test_read_only_agent_without_mutating_tools_is_clean(self, tmp_path: Path) -> None:
        build_harness(
            tmp_path,
            agents={
                "a.md": "---\nname: a\ndescription: Read-only; never merges.\n"
                "tools: Bash, Read, Grep\n---\n\nYou report.\n"
            },
        )
        assert "authority/read-only-contradiction" not in rules(tmp_path)

    def test_agent_without_tools_inherits_everything(self, tmp_path: Path) -> None:
        build_harness(tmp_path, agents={"a.md": "---\nname: a\ndescription: d\n---\n\nBody.\n"})
        assert "authority/agent-inherits-all" in rules(tmp_path)

    def test_preapproved_outward_facing_action_is_an_error(self, tmp_path: Path) -> None:
        build_harness(
            tmp_path,
            settings={"permissions": {"allow": ["Bash(git push:*)"]}},
        )
        got = [f for f in findings(tmp_path) if f.rule == "authority/outward-facing-preapproved"]
        assert got and got[0].severity == hl.ERROR

    def test_read_only_allowlist_entries_are_clean(self, tmp_path: Path) -> None:
        build_harness(
            tmp_path,
            settings={"permissions": {"allow": ["Bash(git status:*)", "Bash(pytest:*)"]}},
        )
        assert "authority/outward-facing-preapproved" not in rules(tmp_path)


class TestCompleteness:
    def test_missing_hook_is_an_error(self, tmp_path: Path) -> None:
        """A registered-but-absent hook fails OPEN — the worst outcome."""
        build_harness(
            tmp_path,
            settings={
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": ".claude/hooks/gone.sh"}]}
                    ]
                }
            },
        )
        got = [f for f in findings(tmp_path) if f.rule == "completeness/hook-missing"]
        assert got and got[0].severity == hl.ERROR

    def test_command_without_description_is_an_error(self, tmp_path: Path) -> None:
        build_harness(tmp_path, commands={"a.md": "Body with no frontmatter.\n"})
        assert "completeness/command-description" in rules(tmp_path)

    def test_argument_hint_only_required_when_arguments_are_read(self, tmp_path: Path) -> None:
        build_harness(
            tmp_path,
            commands={
                "takes.md": "---\ndescription: d\n---\n\nUse $ARGUMENTS.\n",
                "none.md": "---\ndescription: d\n---\n\nNo args here.\n",
            },
        )
        hits = [f for f in findings(tmp_path) if f.rule == "completeness/argument-hint"]
        assert [f.path for f in hits] == [".claude/commands/takes.md"]


class TestBudget:
    def test_oversized_memory_file_is_reported(self, tmp_path: Path) -> None:
        build_harness(tmp_path, memory="x" * 200_000)
        assert "budget/memory-file" in rules(tmp_path)

    def test_descriptions_count_toward_the_always_loaded_total(self, tmp_path: Path) -> None:
        """A command's description is paid for whether or not it is invoked.

        200 commands x 600 chars is ~30k est. tokens, over the 24k default. Each
        description stays under `max_description_chars`, so this isolates the
        aggregate rule rather than tripping the per-description one.
        """
        build_harness(
            tmp_path,
            commands={
                f"c{i}.md": f"---\ndescription: {'d' * 600}\n---\n\nBody.\n" for i in range(200)
            },
        )
        assert "budget/always-loaded" in rules(tmp_path)
        assert "budget/description-length" not in rules(tmp_path)

    def test_a_long_body_is_not_charged_to_the_always_loaded_budget(self, tmp_path: Path) -> None:
        """Bodies load on invocation. Charging them would push people to write
        worse procedures to satisfy a number that does not apply."""
        build_harness(
            tmp_path,
            commands={"a.md": "---\ndescription: short\n---\n\n" + ("word " * 40_000)},
        )
        assert "budget/always-loaded" not in rules(tmp_path)


class TestRedundancy:
    def test_a_rule_in_two_files_is_reported(self, tmp_path: Path) -> None:
        shared = (
            "Never bypass the pre-commit hook with the no verify flag under any circumstances.\n"
        )
        build_harness(
            tmp_path,
            commands={
                "a.md": f"---\ndescription: a\n---\n\n{shared}",
                "b.md": f"---\ndescription: b\n---\n\n{shared}",
            },
        )
        assert "redundancy/duplicate-rule" in rules(tmp_path)

    def test_short_shared_phrases_are_not_duplication(self, tmp_path: Path) -> None:
        build_harness(
            tmp_path,
            commands={
                "a.md": "---\ndescription: a\n---\n\n## Input\n\nRun it.\n",
                "b.md": "---\ndescription: b\n---\n\n## Input\n\nRun it.\n",
            },
        )
        assert "redundancy/duplicate-rule" not in rules(tmp_path)


class TestPlacement:
    """Is each piece of guidance living in the primitive whose job it is?

    Five primitives — instructions file, scoped rules, skills, subagents,
    hooks — and each has one job. Misplacement is the same fail-open shape as
    every other dimension here: a procedure stuffed into the always-loaded file
    is paid for on every turn and read closely on none, a skill with no steps
    applies only when someone thinks to invoke a rule, a scoped file over an
    empty directory stopped applying without saying so, a hook on a misspelled
    event never fires and never complains. Ten rules, and every one of them is
    a markdown structure check, a filesystem fact, or a key lookup — nothing
    here reads intent out of prose. As with the rest of the suite, most of what
    follows is a false-positive guard: the positive case proves the rule can
    fire, the guard proves it does not fire on the tree that looks similar but
    is actually fine.
    """

    # ── no-instruction-file ─────────────────────────────────────────────
    def test_artifacts_without_a_root_instruction_file_are_reported(self, tmp_path: Path) -> None:
        build_harness(
            tmp_path,
            no_memory=True,
            commands={"a.md": "---\ndescription: A\n---\n\nBody.\n"},
        )
        assert "placement/no-instruction-file" in rules(tmp_path)

    def test_a_root_instruction_file_is_clean(self, tmp_path: Path) -> None:
        build_harness(tmp_path, commands={"a.md": "---\ndescription: A\n---\n\nBody.\n"})
        assert "placement/no-instruction-file" not in rules(tmp_path)

    def test_a_tree_with_no_harness_artifacts_at_all_is_clean(self, tmp_path: Path) -> None:
        assert "placement/no-instruction-file" not in rules(tmp_path)

    # ── procedure-in-memory ─────────────────────────────────────────────
    def test_six_consecutive_steps_in_root_memory_is_reported(self, tmp_path: Path) -> None:
        steps = "\n".join(f"{i}. step {i}" for i in range(1, 7))
        build_harness(tmp_path, memory=f"# Project\n\n{steps}\n")
        assert "placement/procedure-in-memory" in rules(tmp_path)

    def test_five_consecutive_steps_is_under_the_boundary(self, tmp_path: Path) -> None:
        steps = "\n".join(f"{i}. step {i}" for i in range(1, 6))
        build_harness(tmp_path, memory=f"# Project\n\n{steps}\n")
        assert "placement/procedure-in-memory" not in rules(tmp_path)

    def test_a_numbered_list_inside_a_fenced_block_is_not_a_procedure(self, tmp_path: Path) -> None:
        """Showing a procedure in an example is not stating one."""
        steps = "\n".join(f"{i}. echo step {i}" for i in range(1, 7))
        build_harness(tmp_path, memory=f"# Project\n\n```bash\n{steps}\n```\n")
        assert "placement/procedure-in-memory" not in rules(tmp_path)

    # ── scope-matches-nothing ───────────────────────────────────────────
    def test_scoped_memory_over_a_directory_with_nothing_else_is_reported(
        self, tmp_path: Path
    ) -> None:
        build_harness(tmp_path)
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "CLAUDE.md").write_text("# Docs\n\nA scoped rule.\n")
        assert "placement/scope-matches-nothing" in rules(tmp_path)

    def test_scoped_memory_beside_real_source_files_is_clean(self, tmp_path: Path) -> None:
        build_harness(tmp_path)
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "CLAUDE.md").write_text("# Docs\n\nA scoped rule.\n")
        (tmp_path / "docs" / "guide.py").write_text("pass\n")
        assert "placement/scope-matches-nothing" not in rules(tmp_path)

    def test_scoped_memory_over_a_directory_holding_only_a_subdirectory_is_clean(
        self, tmp_path: Path
    ) -> None:
        """A subdirectory counts as governed even with nothing directly inside."""
        build_harness(tmp_path)
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "CLAUDE.md").write_text("# Docs\n\nA scoped rule.\n")
        (tmp_path / "docs" / "sub").mkdir()
        (tmp_path / "docs" / "sub" / "guide.py").write_text("pass\n")
        assert "placement/scope-matches-nothing" not in rules(tmp_path)

    # ── scope-not-declared ──────────────────────────────────────────────
    def test_scope_key_required_by_the_runner_but_missing_is_reported(self, tmp_path: Path) -> None:
        build_harness(tmp_path, no_memory=True)
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "CLAUDE.md").write_text("# Docs\n\nA scoped rule.\n")
        (tmp_path / "docs" / "guide.py").write_text("pass\n")
        (tmp_path / "harnesslint.toml").write_text('[runner]\nscope_key = "globs"\n')
        assert "placement/scope-not-declared" in rules(tmp_path)

    def test_a_declared_scope_key_is_clean(self, tmp_path: Path) -> None:
        build_harness(tmp_path, no_memory=True)
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "CLAUDE.md").write_text(
            "---\nglobs: docs/**\n---\n\n# Docs\n\nA scoped rule.\n"
        )
        (tmp_path / "docs" / "guide.py").write_text("pass\n")
        (tmp_path / "harnesslint.toml").write_text('[runner]\nscope_key = "globs"\n')
        assert "placement/scope-not-declared" not in rules(tmp_path)

    def test_scope_not_declared_is_inert_under_the_stock_claude_code_profile(
        self, tmp_path: Path
    ) -> None:
        """claude-code declares scope by directory placement, not frontmatter —
        `scope_key` is empty, so this rule has nothing to check."""
        build_harness(tmp_path, no_memory=True)
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "CLAUDE.md").write_text("# Docs\n\nA scoped rule.\n")
        (tmp_path / "docs" / "guide.py").write_text("pass\n")
        assert "placement/scope-not-declared" not in rules(tmp_path)

    # ── skill-without-steps ─────────────────────────────────────────────
    def test_a_skill_with_prose_only_is_reported(self, tmp_path: Path) -> None:
        build_harness(
            tmp_path,
            skills={"demo/SKILL.md": "---\ndescription: d\n---\n\nJust prose, no procedure.\n"},
        )
        assert "placement/skill-without-steps" in rules(tmp_path)

    def test_a_skill_with_step_headings_is_clean(self, tmp_path: Path) -> None:
        build_harness(
            tmp_path,
            skills={
                "demo/SKILL.md": "---\ndescription: d\n---\n\n"
                "## Step 1\n\nDo the thing.\n\n## Step 2\n\nDo the next thing.\n"
            },
        )
        assert "placement/skill-without-steps" not in rules(tmp_path)

    def test_a_skill_with_an_ordered_list_is_clean(self, tmp_path: Path) -> None:
        build_harness(
            tmp_path,
            skills={"demo/SKILL.md": "---\ndescription: d\n---\n\n1. Do the thing.\n2. Done.\n"},
        )
        assert "placement/skill-without-steps" not in rules(tmp_path)

    def test_a_nested_skill_is_discovered_under_the_widened_glob(self, tmp_path: Path) -> None:
        """`.claude/skills/*/SKILL.md` widened to `**/SKILL.md` this release —
        before that, a nested skill got zero checks and reported clean, which
        is exactly the failure mode this tool exists to name."""
        build_harness(
            tmp_path,
            skills={"group/nested/SKILL.md": "---\ndescription: d\n---\n\nJust prose.\n"},
        )
        got = [f for f in findings(tmp_path) if f.path == ".claude/skills/group/nested/SKILL.md"]
        assert got and got[0].rule == "placement/skill-without-steps"

    # ── agent-tool-unknown ───────────────────────────────────────────────
    def test_a_typoed_tool_grant_is_reported(self, tmp_path: Path) -> None:
        build_harness(
            tmp_path,
            agents={"a.md": "---\nname: a\ndescription: d\ntools: Wrte\n---\n\nBody.\n"},
        )
        assert "placement/agent-tool-unknown" in rules(tmp_path)

    def test_an_mcp_tool_grant_is_not_treated_as_unknown(self, tmp_path: Path) -> None:
        build_harness(
            tmp_path,
            agents={
                "a.md": "---\nname: a\ndescription: d\n"
                "tools: mcp__github__create_pull_request\n---\n\nBody.\n"
            },
        )
        assert "placement/agent-tool-unknown" not in rules(tmp_path)

    def test_an_extra_tool_declared_in_config_is_not_unknown(self, tmp_path: Path) -> None:
        build_harness(
            tmp_path,
            agents={"a.md": "---\nname: a\ndescription: d\ntools: MyCustomTool\n---\n\nBody.\n"},
        )
        (tmp_path / "harnesslint.toml").write_text('[placement]\nextra_tools = ["MyCustomTool"]\n')
        assert "placement/agent-tool-unknown" not in rules(tmp_path)

    def test_legitimate_known_tools_are_clean(self, tmp_path: Path) -> None:
        build_harness(
            tmp_path,
            agents={"a.md": "---\nname: a\ndescription: d\ntools: Read, Grep\n---\n\nBody.\n"},
        )
        assert "placement/agent-tool-unknown" not in rules(tmp_path)

    # ── agent-body-oversized ────────────────────────────────────────────
    def test_an_agent_body_over_the_configured_budget_is_reported(self, tmp_path: Path) -> None:
        build_harness(
            tmp_path,
            agents={
                "a.md": "---\nname: a\ndescription: d\n---\n\n"
                "This body is a good deal longer than the tiny budget below.\n"
            },
        )
        (tmp_path / "harnesslint.toml").write_text("[placement]\nmax_agent_tokens = 10\n")
        assert "placement/agent-body-oversized" in rules(tmp_path)

    def test_a_short_agent_body_is_clean(self, tmp_path: Path) -> None:
        build_harness(tmp_path, agents={"a.md": "---\nname: a\ndescription: d\n---\n\nBody.\n"})
        assert "placement/agent-body-oversized" not in rules(tmp_path)

    # ── hook-unknown-event ──────────────────────────────────────────────
    def test_an_unknown_hook_event_is_reported(self, tmp_path: Path) -> None:
        build_harness(tmp_path, settings={"hooks": {"PreToolUze": []}})
        assert "placement/hook-unknown-event" in rules(tmp_path)

    def test_every_real_hook_event_is_clean(self, tmp_path: Path) -> None:
        events = hl.PROFILES["claude-code"].hook_events
        build_harness(tmp_path, settings={"hooks": {name: [] for name in events}})
        assert "placement/hook-unknown-event" not in rules(tmp_path)

    # ── no-hooks-registered ──────────────────────────────────────────────
    def test_an_empty_hooks_block_is_reported(self, tmp_path: Path) -> None:
        build_harness(tmp_path, settings={"hooks": {}})
        assert "placement/no-hooks-registered" in rules(tmp_path)

    def test_one_registered_hook_is_clean(self, tmp_path: Path) -> None:
        build_harness(
            tmp_path,
            settings={
                "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}
            },
        )
        assert "placement/no-hooks-registered" not in rules(tmp_path)

    def test_no_settings_file_at_all_is_clean(self, tmp_path: Path) -> None:
        """A runner that was never configured has nothing to report here."""
        build_harness(tmp_path)
        assert "placement/no-hooks-registered" not in rules(tmp_path)
        assert "placement/hook-unknown-event" not in rules(tmp_path)

    # ── primitives-unavailable ──────────────────────────────────────────
    def test_agents_md_reports_the_primitives_it_has_no_concept_of(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("# Root\n\nA rule.\n")
        assert "placement/primitives-unavailable" in rules(tmp_path)

    def test_claude_code_has_all_five_primitives_and_does_not_fire(self, tmp_path: Path) -> None:
        build_harness(tmp_path)
        assert "placement/primitives-unavailable" not in rules(tmp_path)

    # ── partial measurement under a thinner profile ─────────────────────
    def test_placement_is_partially_measured_under_agents_md(self, tmp_path: Path) -> None:
        """agents-md has no agents, skills, or hooks, so those rule families
        cannot speak — but the memory rules still can, and the gap itself is
        reported. Partial measurement, not silence."""
        steps = "\n".join(f"{i}. step {i}" for i in range(1, 7))
        (tmp_path / "AGENTS.md").write_text(f"# Root\n\n{steps}\n")
        got = rules(tmp_path)
        assert "placement/primitives-unavailable" in got
        assert "placement/procedure-in-memory" in got
        for absent in (
            "placement/agent-tool-unknown",
            "placement/agent-body-oversized",
            "placement/skill-without-steps",
            "placement/hook-unknown-event",
            "placement/no-hooks-registered",
        ):
            assert absent not in got


# ── the ratchet ───────────────────────────────────────────────────────────
class TestBaselineRatchet:
    """Regression, not perfection — the property that makes this adoptable."""

    def test_baselined_findings_do_not_fail(self, tmp_path: Path) -> None:
        build_harness(tmp_path, agents={"a.md": "---\nname: a\ndescription: d\n---\n\nBody.\n"})
        got = findings(tmp_path)
        assert got
        new, worse = hl.compare(got, hl.build_baseline(got))
        assert not new and not worse

    def test_a_new_finding_is_caught(self, tmp_path: Path) -> None:
        build_harness(tmp_path, agents={"a.md": "---\nname: a\ndescription: d\n---\n\nBody.\n"})
        baseline = hl.build_baseline(findings(tmp_path))
        (tmp_path / ".claude" / "agents" / "b.md").write_text(
            "---\nname: b\ndescription: d\n---\n\nBody.\n"
        )
        new, worse = hl.compare(findings(tmp_path), baseline)
        assert new and worse

    def test_a_stale_ruleset_version_invalidates_the_baseline(self, tmp_path: Path) -> None:
        """A baseline recorded under an older meaning must not suppress a rule."""
        build_harness(tmp_path)
        (tmp_path / ".harnesslint-baseline.json").write_text(
            json.dumps(
                {"ruleset_version": hl.RULESET_VERSION - 1, "counts": {}, "fingerprints": []}
            )
        )
        proc = _cli(tmp_path, "check")
        assert "ruleset" in proc.stderr


# ── cli contract ──────────────────────────────────────────────────────────
def _cli(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_TOOL), "--root", str(root), *args],
        capture_output=True,
        text=True,
        timeout=120,
    )


class TestCli:
    def test_report_never_fails_the_build(self, tmp_path: Path) -> None:
        build_harness(tmp_path, agents={"a.md": "---\nname: a\ndescription: d\n---\n\nB.\n"})
        assert _cli(tmp_path, "report").returncode == 0

    def test_strict_fails_on_an_error(self, tmp_path: Path) -> None:
        build_harness(tmp_path, settings={"permissions": {"allow": ["Bash(git push:*)"]}})
        assert _cli(tmp_path, "check", "--strict").returncode == 1

    def test_json_output_parses(self, tmp_path: Path) -> None:
        build_harness(tmp_path)
        proc = _cli(tmp_path, "report", "--format", "json")
        payload = json.loads(proc.stdout)
        assert payload["ruleset_version"] == hl.RULESET_VERSION
        assert "findings" in payload

    def test_github_format_emits_annotations(self, tmp_path: Path) -> None:
        build_harness(tmp_path, settings={"permissions": {"allow": ["Bash(git push:*)"]}})
        proc = _cli(tmp_path, "report", "--format", "github")
        assert "::error file=" in proc.stdout

    def test_baseline_roundtrip(self, tmp_path: Path) -> None:
        build_harness(tmp_path, agents={"a.md": "---\nname: a\ndescription: d\n---\n\nB.\n"})
        assert _cli(tmp_path, "baseline").returncode == 0
        assert (tmp_path / ".harnesslint-baseline.json").is_file()
        assert _cli(tmp_path, "check").returncode == 0

    def test_runs_on_a_repo_with_no_harness_at_all(self, tmp_path: Path) -> None:
        """Portability floor: an empty repo must not crash it."""
        assert _cli(tmp_path, "report").returncode == 0

    def test_unknown_rule_explain_is_a_usage_error(self, tmp_path: Path) -> None:
        assert _cli(tmp_path, "explain", "nope/nope").returncode == 2


class TestProjectRules:
    """The plugin seam that keeps this useful as conventions accumulate."""

    def test_a_project_rule_is_loaded_and_reported(self, tmp_path: Path) -> None:
        build_harness(tmp_path)
        rules_dir = tmp_path / ".harnesslint" / "rules"
        rules_dir.mkdir(parents=True)
        # No sys.path hack: load_project_rules puts the tool's own directory on
        # the path precisely so a vendored copy needs none. If that ever breaks,
        # this test is what notices.
        (rules_dir / "r.py").write_text(
            "from harnesslint import Finding, ERROR\n"
            "def check(h):\n"
            "    return [Finding('project', 'project/demo', ERROR, 'x.md', 'demo')]\n"
        )
        assert "project/demo" in rules(tmp_path)

    def test_a_broken_project_rule_reports_itself_and_does_not_mask_the_rest(
        self, tmp_path: Path
    ) -> None:
        build_harness(tmp_path, settings={"permissions": {"allow": ["Bash(git push:*)"]}})
        rules_dir = tmp_path / ".harnesslint" / "rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / "boom.py").write_text("def check(h):\n    raise ValueError('boom')\n")
        got = rules(tmp_path)
        assert "project/rule-error" in got
        assert "authority/outward-facing-preapproved" in got


class TestSelfCheck:
    """The tool, pointed at its own project."""

    def test_runs_clean_on_this_repo(self) -> None:
        """harnesslint has no .claude/ harness of its own, which is the point:
        a repo with nothing to measure must report nothing and exit 0."""
        proc = _cli(_ROOT, "check")
        assert proc.returncode == 0, proc.stdout + proc.stderr

    def test_init_is_idempotent(self, tmp_path: Path) -> None:
        assert _cli(tmp_path, "init").returncode == 0
        first = (tmp_path / "harnesslint.toml").read_text()
        assert _cli(tmp_path, "init").returncode == 0
        assert (tmp_path / "harnesslint.toml").read_text() == first

    def test_init_scaffolds_a_loadable_rule(self, tmp_path: Path) -> None:
        """The scaffolded rule must import and run, or `init` is a trap."""
        build_harness(tmp_path, memory="# P\n\nTODO: decide the retry policy.\n")
        _cli(tmp_path, "init")
        proc = _cli(tmp_path, "report", "--format", "json")
        got = {f["rule"] for f in json.loads(proc.stdout)["findings"]}
        assert "project/todo-in-harness" in got
        assert "project/rule-error" not in got


# ── configuration ─────────────────────────────────────────────────────────
class TestConfig:
    """The layer everything else is steered by, and the one with no coverage.

    `severities` and `ignore` both change what CI does with a finding, so a
    regression here is silent by construction: the tool keeps exiting 0.
    """

    def test_absent_config_is_a_valid_state(self, tmp_path: Path) -> None:
        build_harness(tmp_path)
        assert hl.load_config(tmp_path)["budget"]["always_loaded_tokens"] == 24000

    def test_deep_merge_keeps_untouched_siblings(self) -> None:
        base = {"a": {"x": 1, "y": 2}, "b": 3}
        assert hl._deep_merge(base, {"a": {"y": 9}}) == {"a": {"x": 1, "y": 9}, "b": 3}

    def test_loading_a_config_does_not_mutate_the_defaults(self, tmp_path: Path) -> None:
        """DEFAULTS is a module global; a shallow copy would let a repo poison it."""
        build_harness(tmp_path)
        (tmp_path / "harnesslint.toml").write_text("[budget]\nalways_loaded_tokens = 1\n")
        hl.load_config(tmp_path)
        assert hl.DEFAULTS["budget"]["always_loaded_tokens"] == 24000

    def test_a_budget_override_is_applied(self, tmp_path: Path) -> None:
        build_harness(tmp_path, memory="x" * 40_000)
        assert "budget/memory-file" not in rules(tmp_path)
        (tmp_path / "harnesslint.toml").write_text("[budget]\nmemory_file_tokens = 100\n")
        assert "budget/memory-file" in rules(tmp_path)

    def test_severity_override_changes_the_severity_not_the_finding(self, tmp_path: Path) -> None:
        build_harness(tmp_path, settings={"permissions": {"allow": ["Bash(git push:*)"]}})
        (tmp_path / "harnesslint.toml").write_text(
            '[severities]\n"authority/outward-facing-preapproved" = "info"\n'
        )
        got = [f for f in findings(tmp_path) if f.rule == "authority/outward-facing-preapproved"]
        assert got and got[0].severity == hl.INFO

    def test_ignore_removes_a_rule_entirely(self, tmp_path: Path) -> None:
        build_harness(tmp_path, settings={"permissions": {"allow": ["Bash(git push:*)"]}})
        (tmp_path / "harnesslint.toml").write_text(
            'ignore = ["authority/outward-facing-preapproved"]\n'
        )
        assert "authority/outward-facing-preapproved" not in rules(tmp_path)

    def test_ignore_can_be_scoped_to_one_path(self, tmp_path: Path) -> None:
        build_harness(
            tmp_path,
            agents={
                "a.md": "---\nname: a\ndescription: d\n---\n\nB.\n",
                "b.md": "---\nname: b\ndescription: d\n---\n\nB.\n",
            },
        )
        (tmp_path / "harnesslint.toml").write_text(
            'ignore = ["authority/agent-inherits-all:.claude/agents/a.md"]\n'
        )
        hits = [f.path for f in findings(tmp_path) if f.rule == "authority/agent-inherits-all"]
        assert hits == [".claude/agents/b.md"]

    def test_declaring_a_cli_silences_it(self, tmp_path: Path) -> None:
        build_harness(
            tmp_path,
            commands={"a.md": "---\ndescription: A\n---\n\n```bash\nkubectl get pods\n```\n"},
        )
        assert "executability/undeclared-cli" in rules(tmp_path)
        (tmp_path / "harnesslint.toml").write_text('[executability]\ndeclared_clis = ["kubectl"]\n')
        assert "executability/undeclared-cli" not in rules(tmp_path)
