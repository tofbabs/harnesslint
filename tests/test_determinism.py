"""tests/test_determinism.py — the contract, made executable.

The README promises: same tree + same profile + same config + same ruleset
version -> identical bytes, on any machine, any supported Python, any hash seed.

That promise is the only reason the ratchet is worth anything. A fingerprint
that moves between a laptop and CI makes every run read as drift, and a gate
that cries drift on every run is one people route around within a week.

These tests exist because the promise was already broken. `bash -n` was invoked
with an absolute path and its stderr went straight into the finding message, so
the message — and therefore the fingerprint — carried the checkout location. A
baseline recorded on a laptop could not match CI. `ast.parse`'s SyntaxError text
went into a message the same way; that wording is an interpreter implementation
detail CPython has reworded across releases before, so the fingerprint was
hostage to which Python ran the check.

Neither was visible to a test that ran the tool twice in one directory on one
interpreter, which is what the suite had. Determinism is not a property you
confirm by repeating yourself; it is one you confirm by varying everything that
is supposed not to matter.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TOOL = _ROOT / "harnesslint.py"

sys.path.insert(0, str(_ROOT))
import harnesslint as hl  # noqa: E402


def build_hostile_harness(root: Path) -> Path:
    """A harness containing every construct that has leaked the environment.

    A broken Python hook (interpreter-version-dependent message), a broken shell
    hook (shell-version- and path-dependent message), and a hook that parses but
    is not executable (filesystem-dependent).
    """
    (root / ".claude" / "commands").mkdir(parents=True)
    (root / ".claude" / "hooks").mkdir(parents=True)
    (root / "CLAUDE.md").write_text("# P\n\nA rule that is long enough to be a rule here.\n")
    (root / ".claude" / "commands" / "a.md").write_text(
        "---\ndescription: A\n---\n\n```bash\nterraform apply\n```\n"
    )
    (root / ".claude" / "hooks" / "broken.py").write_text("def f(:\n")
    (root / ".claude" / "hooks" / "broken.sh").write_text("if true; then\n")
    (root / ".claude" / "hooks" / "fine.py").write_text("print('ok')\n")
    (root / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "hooks": [
                                {"type": "command", "command": ".claude/hooks/broken.py"},
                                {"type": "command", "command": ".claude/hooks/broken.sh"},
                                {"type": "command", "command": ".claude/hooks/fine.py"},
                            ]
                        }
                    ]
                }
            },
            indent=2,
        )
    )
    # Shell checking on, so the bash path is genuinely exercised rather than
    # skipped — the leak this suite is guarding against lives behind that flag.
    (root / "harnesslint.toml").write_text("[executability]\nshell_syntax_check = true\n")
    return root


def _run(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(
        [sys.executable, str(_TOOL), "--root", str(root), *args],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, **(env or {})},
    )
    assert proc.returncode in (0, 1), proc.stderr
    return proc.stdout


class TestDeterminismContract:
    def test_identical_across_repo_locations(self, tmp_path: Path) -> None:
        """The same harness at two paths must produce the same bytes.

        This is the test that catches an absolute path reaching a message. A
        run-it-twice test cannot: it uses the same directory both times.
        """
        a = build_hostile_harness(tmp_path / "one")
        b = tmp_path / "a-much-longer-directory-name-two"
        shutil.copytree(a, b)

        assert _run(a, "report", "--format", "json") == _run(b, "report", "--format", "json")

    def test_identical_across_hash_seeds(self, tmp_path: Path) -> None:
        """Sets and dicts are everywhere in here; none may reach the output."""
        root = build_hostile_harness(tmp_path)
        first = _run(root, "report", "--format", "json", env={"PYTHONHASHSEED": "0"})
        second = _run(root, "report", "--format", "json", env={"PYTHONHASHSEED": "524287"})
        assert first == second

    def test_identical_across_file_creation_order(self, tmp_path: Path) -> None:
        """Discovery must sort, not follow the filesystem's own ordering."""
        names = ["alpha.md", "beta.md", "gamma.md", "delta.md"]
        outputs = []
        for order in (names, list(reversed(names))):
            root = tmp_path / f"tree-{order[0]}"
            (root / ".claude" / "commands").mkdir(parents=True)
            (root / "CLAUDE.md").write_text("# P\n")
            for name in order:
                (root / ".claude" / "commands" / name).write_text("Body without frontmatter.\n")
            outputs.append(_run(root, "report", "--format", "json"))
        assert outputs[0] == outputs[1]

    def test_no_finding_leaks_an_absolute_path(self, tmp_path: Path) -> None:
        """The testable proxy for cross-machine fingerprint stability.

        Anything machine-specific in a message is also in the fingerprint, since
        `_SALIENT` strips only digits and quoted spans.
        """
        root = build_hostile_harness(tmp_path)
        payload = json.loads(_run(root, "report", "--format", "json"))
        leaked = [
            f
            for f in payload["findings"]
            if str(tmp_path) in f["message"] or "/private/" in f["message"]
        ]
        assert not leaked, leaked

    def test_hook_syntax_message_carries_no_interpreter_text(self, tmp_path: Path) -> None:
        """`ast.parse` rewrites its messages between releases; ours must not move."""
        root = build_hostile_harness(tmp_path)
        payload = json.loads(_run(root, "report", "--format", "json"))
        syntax = [f for f in payload["findings"] if f["rule"] == "executability/hook-syntax"]
        assert syntax
        for f in syntax:
            assert "<unknown>" not in f["message"]
            assert "invalid syntax" not in f["message"]
            assert f["message"].startswith("does not parse (")

    def test_json_output_is_key_sorted(self, tmp_path: Path) -> None:
        """A diff of two JSON reports has to be readable to be worth emitting."""
        root = build_hostile_harness(tmp_path)
        raw = _run(root, "report", "--format", "json")
        assert raw == json.dumps(json.loads(raw), indent=2, sort_keys=True) + "\n"

    def test_crlf_and_lf_cost_the_same(self, tmp_path: Path) -> None:
        """A Windows checkout must not read as a budget regression."""
        body = "# Project\n" + ("A sentence of perfectly ordinary prose here.\n" * 200)
        lf = tmp_path / "lf"
        crlf = tmp_path / "crlf"
        for root, text in ((lf, body), (crlf, body.replace("\n", "\r\n"))):
            (root / ".claude").mkdir(parents=True)
            (root / "CLAUDE.md").write_bytes(text.encode())
        cfg = hl.load_config(lf)
        cpt = cfg["budget"]["chars_per_token"]
        lf_doc = hl.discover(lf, cfg).memory[0]
        crlf_doc = hl.discover(crlf, hl.load_config(crlf)).memory[0]
        assert hl.estimate_tokens(lf_doc.raw, cpt) == hl.estimate_tokens(crlf_doc.raw, cpt)


class TestPathDependenceIsDeclared:
    """Where a check cannot run without depending on the environment, it says so."""

    def test_shell_check_is_off_by_default_and_says_so(self, tmp_path: Path) -> None:
        root = build_hostile_harness(tmp_path)
        (root / "harnesslint.toml").unlink()  # back to defaults
        got = {f.rule for f in hl.run(hl.discover(root, hl.load_config(root)))}
        assert "executability/shell-syntax-unchecked" in got
        assert "executability/hook-syntax" in got  # the .py hook is still checked in-process

    def test_shell_check_reports_when_enabled(self, tmp_path: Path) -> None:
        root = build_hostile_harness(tmp_path)
        found = [
            f
            for f in hl.run(hl.discover(root, hl.load_config(root)))
            if f.rule == "executability/hook-syntax" and f.path.endswith(".sh")
        ]
        assert found and found[0].severity == hl.ERROR
        assert "shell syntax error" in found[0].message

    def test_exec_bit_check_can_be_forced_off(self, tmp_path: Path) -> None:
        root = build_hostile_harness(tmp_path)
        (root / "harnesslint.toml").write_text('[executability]\nexec_bit_check = "off"\n')
        got = {f.rule for f in hl.run(hl.discover(root, hl.load_config(root)))}
        assert "executability/hook-not-executable" not in got
        assert "executability/exec-bit-unverifiable" in got

    def test_exec_bit_is_reported_when_readable(self, tmp_path: Path) -> None:
        root = build_hostile_harness(tmp_path)
        (root / "harnesslint.toml").write_text('[executability]\nexec_bit_check = "on"\n')
        found = [
            f
            for f in hl.run(hl.discover(root, hl.load_config(root)))
            if f.rule == "executability/hook-not-executable"
        ]
        assert found and found[0].severity == hl.ERROR


class TestSettingsFailOpen:
    """The checker must not do the thing it exists to catch."""

    def test_unparseable_settings_is_reported_not_swallowed(self, tmp_path: Path) -> None:
        (tmp_path / ".claude").mkdir(parents=True)
        (tmp_path / "CLAUDE.md").write_text("# P\n")
        (tmp_path / ".claude" / "settings.json").write_text('{"permissions": {,,}')
        got = [
            f
            for f in hl.run(hl.discover(tmp_path, hl.load_config(tmp_path)))
            if f.rule == "completeness/settings-unparseable"
        ]
        assert got and got[0].severity == hl.ERROR
