"""tests/test_profiles.py — the runner-agnostic seam.

Two things have to hold at once, and they pull in opposite directions:

  * A Claude Code repo must see exactly what it saw before profiles existed.
    The refactor is only safe if it changed nothing for the harness the tool was
    built against, and `TestClaudeCodeRegressionLock` is the evidence rather
    than the claim.
  * A repo on any other runner must get an honest answer instead of seven
    dimensions quietly measuring globs that match nothing. A profile that checks
    almost nothing has to say which dimensions it cannot speak to, or a short
    report reads as a clean harness.
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


def _cli(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_TOOL), "--root", str(root), *args],
        capture_output=True,
        text=True,
        timeout=120,
    )


class TestDetection:
    def test_claude_layout_selects_claude_code(self, tmp_path: Path) -> None:
        (tmp_path / ".claude" / "commands").mkdir(parents=True)
        assert hl.detect_profile(tmp_path).name == "claude-code"

    def test_agents_md_selects_agents_md(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("# Agents\n")
        assert hl.detect_profile(tmp_path).name == "agents-md"

    def test_claude_wins_when_both_are_present(self, tmp_path: Path) -> None:
        """Ordered probe: the runner with an actual layout beats the convention."""
        (tmp_path / ".claude").mkdir()
        (tmp_path / "AGENTS.md").write_text("# Agents\n")
        assert hl.detect_profile(tmp_path).name == "claude-code"

    def test_bare_repo_falls_back_to_generic(self, tmp_path: Path) -> None:
        assert hl.detect_profile(tmp_path).name == "generic"

    def test_detection_is_a_function_of_the_tree(self, tmp_path: Path) -> None:
        (tmp_path / ".claude").mkdir()
        assert {hl.detect_profile(tmp_path).name for _ in range(5)} == {"claude-code"}


class TestExplicitProfile:
    def test_config_overrides_detection(self, tmp_path: Path) -> None:
        (tmp_path / ".claude" / "agents").mkdir(parents=True)
        (tmp_path / "harnesslint.toml").write_text('[runner]\nprofile = "generic"\n')
        assert hl.load_config(tmp_path)["runner"]["profile"] == "generic"

    def test_an_unknown_profile_is_a_usage_error(self, tmp_path: Path) -> None:
        (tmp_path / "harnesslint.toml").write_text('[runner]\nprofile = "nope"\n')
        proc = _cli(tmp_path, "report")
        assert proc.returncode == 2
        assert "unknown runner profile" in proc.stderr

    def test_paths_in_config_still_override_the_profile(self, tmp_path: Path) -> None:
        """The profile sits between defaults and the TOML, not on top of it."""
        (tmp_path / "ops").mkdir()
        (tmp_path / "ops" / "deploy.md").write_text("Body with no frontmatter.\n")
        (tmp_path / ".claude").mkdir()
        (tmp_path / "harnesslint.toml").write_text('[paths]\ncommands = ["ops/**/*.md"]\n')
        got = hl.discover(tmp_path, hl.load_config(tmp_path))
        assert [d.rel for d in got.commands] == ["ops/deploy.md"]

    def test_malformed_toml_is_a_usage_error(self, tmp_path: Path) -> None:
        (tmp_path / "harnesslint.toml").write_text("[runner\nprofile =\n")
        proc = _cli(tmp_path, "report")
        assert proc.returncode == 2
        assert "not valid TOML" in proc.stderr


class TestClaudeCodeRegressionLock:
    """A Claude harness must measure exactly what it measured before profiles.

    The expected set below was captured from the pre-profile implementation. It
    is deliberately spelled out rather than computed, so that a future change to
    the profile cannot quietly move the goalposts and still pass.

    The set grew by exactly one entry when `placement` shipped:
    `placement/skill-without-steps` on `.claude/skills/demo/SKILL.md`. That is
    a genuinely earned finding, not drift — the fixture's skill body is `B.`,
    which states no steps, so a dimension that measures placement is right to
    name it.
    """

    EXPECTED = {
        ("authority/agent-inherits-all", ".claude/agents/wide.md"),
        ("authority/outward-facing-preapproved", ".claude/settings.json"),
        ("authority/read-only-contradiction", ".claude/agents/auditor.md"),
        ("completeness/argument-hint", ".claude/commands/a.md"),
        ("completeness/command-description", ".claude/commands/b.md"),
        ("completeness/hook-missing", ".claude/settings.json"),
        ("completeness/skill-description", ".claude/skills/demo/SKILL.md"),
        ("executability/undeclared-cli", ".claude/commands/a.md"),
        ("placement/skill-without-steps", ".claude/skills/demo/SKILL.md"),
        ("redundancy/duplicate-rule", ".claude/commands/a.md"),
        ("references/broken-link", "CLAUDE.md"),
    }

    @staticmethod
    def build(root: Path) -> Path:
        (root / ".claude" / "commands").mkdir(parents=True)
        (root / ".claude" / "agents").mkdir(parents=True)
        (root / ".claude" / "skills" / "demo").mkdir(parents=True)
        shared = (
            "Never bypass the pre-commit hook with the no verify flag under any circumstances.\n"
        )
        (root / "CLAUDE.md").write_text(f"# Project\n\n{shared}\nSee [gone](docs/gone.md).\n")
        (root / ".claude" / "commands" / "a.md").write_text(
            f"---\ndescription: A\n---\n\n{shared}\nUse $ARGUMENTS.\n\n"
            "```bash\ngh issue view 1\n```\n"
        )
        (root / ".claude" / "commands" / "b.md").write_text("Body with no frontmatter.\n")
        (root / ".claude" / "agents" / "auditor.md").write_text(
            "---\nname: auditor\ndescription: Read-only; never merges.\n"
            "tools: Bash, Read, Write\n---\n\nYou report.\n"
        )
        (root / ".claude" / "agents" / "wide.md").write_text(
            "---\nname: wide\ndescription: d\n---\n\nBody.\n"
        )
        (root / ".claude" / "skills" / "demo" / "SKILL.md").write_text(
            "---\nname: demo\n---\n\nB.\n"
        )
        (root / ".claude" / "settings.json").write_text(
            json.dumps(
                {
                    "permissions": {"allow": ["Bash(git push:*)"]},
                    "hooks": {
                        "SessionStart": [
                            {"hooks": [{"type": "command", "command": ".claude/hooks/gone.sh"}]}
                        ]
                    },
                }
            )
        )
        return root

    def test_profile_is_claude_code(self, tmp_path: Path) -> None:
        root = self.build(tmp_path)
        assert hl.discover(root, hl.load_config(root)).profile == "claude-code"

    def test_finding_set_is_unchanged(self, tmp_path: Path) -> None:
        root = self.build(tmp_path)
        got = {
            (f.rule, f.path)
            for f in hl.run(hl.discover(root, hl.load_config(root)))
            if f.severity != hl.INFO
        }
        assert got == self.EXPECTED

    def test_no_dimension_is_inert_under_claude_code(self) -> None:
        assert hl.PROFILES["claude-code"].inert_dimensions == []


class TestOtherProfiles:
    def test_agents_md_finds_nested_instruction_files(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("# Root\n")
        (tmp_path / "svc").mkdir()
        (tmp_path / "svc" / "AGENTS.md").write_text("# Scoped\n")
        got = hl.discover(tmp_path, hl.load_config(tmp_path))
        assert got.profile == "agents-md"
        assert [d.rel for d in got.memory] == ["AGENTS.md", "svc/AGENTS.md"]

    def test_runner_specific_checks_are_off_not_silently_clean(self, tmp_path: Path) -> None:
        """The dimensions a profile cannot speak to are named, not hidden."""
        (tmp_path / "AGENTS.md").write_text("# Root\n")
        inert = hl.PROFILES["agents-md"].inert_dimensions
        assert set(inert) == {"completeness", "authority", "executability"}
        proc = _cli(tmp_path, "report")
        assert "not measured (profile)" in proc.stdout

    def test_generic_still_reports_the_runner_neutral_dimensions(self, tmp_path: Path) -> None:
        """budget, redundancy and references need no runner at all."""
        (tmp_path / "CLAUDE.md").write_text("# P\n\nSee [gone](docs/gone.md).\n")
        (tmp_path / "harnesslint.toml").write_text('[runner]\nprofile = "generic"\n')
        got = {f.rule for f in hl.run(hl.discover(tmp_path, hl.load_config(tmp_path)))}
        assert "references/broken-link" in got

    def test_empty_repo_is_clean_under_generic(self, tmp_path: Path) -> None:
        proc = _cli(tmp_path, "report")
        assert proc.returncode == 0
        assert "profile generic" in proc.stdout


class TestProfileAndTheRatchet:
    def test_baseline_records_the_profile(self, tmp_path: Path) -> None:
        TestClaudeCodeRegressionLock.build(tmp_path)
        assert _cli(tmp_path, "baseline").returncode == 0
        data = json.loads((tmp_path / ".harnesslint-baseline.json").read_text())
        assert data["profile"] == "claude-code"

    def test_a_profile_change_invalidates_the_baseline(self, tmp_path: Path) -> None:
        """A silent profile flip must not grandfather findings nobody accepted."""
        TestClaudeCodeRegressionLock.build(tmp_path)
        _cli(tmp_path, "baseline")
        (tmp_path / "harnesslint.toml").write_text('[runner]\nprofile = "generic"\n')
        proc = _cli(tmp_path, "check")
        assert "profile" in proc.stderr
        assert "re-run" in proc.stderr


class TestProfilesCommand:
    def test_lists_every_profile_and_marks_the_detected_one(self, tmp_path: Path) -> None:
        (tmp_path / ".claude").mkdir()
        proc = _cli(tmp_path, "profiles")
        assert proc.returncode == 0
        for name in hl.PROFILES:
            assert name in proc.stdout
        assert "* claude-code" in proc.stdout

    def test_states_what_each_profile_cannot_measure(self, tmp_path: Path) -> None:
        proc = _cli(tmp_path, "profiles")
        assert "not measured:" in proc.stdout
        assert "all seven apply" in proc.stdout
