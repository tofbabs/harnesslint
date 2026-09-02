"""
tests/test_harnesslint.py — regression for the six built-in dimensions.

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
def build_harness(root: Path, *, commands=None, agents=None, memory=None, settings=None) -> Path:
    """Write a synthetic harness and return its root."""
    (root / ".claude" / "commands").mkdir(parents=True, exist_ok=True)
    (root / ".claude" / "agents").mkdir(parents=True, exist_ok=True)
    (root / "CLAUDE.md").write_text(memory or "# Project\n\nA rule.\n")
    for name, text in (commands or {}).items():
        (root / ".claude" / "commands" / name).write_text(text)
    for name, text in (agents or {}).items():
        (root / ".claude" / "agents" / name).write_text(text)
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
