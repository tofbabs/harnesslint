#!/usr/bin/env python3
"""
harnesslint — measure drift in an agent harness, deterministically.

An agent harness — the instruction file, slash commands, sub-agents, skills,
hooks and tool grants that steer a coding agent — is executable configuration
that nothing validates. It rots the way documentation rots: silently, and in
the direction of looking fine.

This tool was extracted from the audit of one real harness, where four defects
had accumulated unnoticed. Every one of them shared a shape worth naming,
because it is the shape this whole tool is aimed at: **each failed open and
reported success.**

  * A stale project-board id that the CLI accepted with exit 0, while the card
    silently went nowhere.
  * A test gate that was never installed, so commits passed through a hook that
    did not exist.
  * A stale org slug, masked by a redirect, working right up until it wouldn't.
  * Commands built on a CLI absent from half the environments they ran in.

None was visible until someone read every harness file at once. That is a job
for a checker, and it is the one thing a checker is reliably better at than a
person.

So: measure a harness across seven dimensions, print the state, and compare it
against a committed baseline so CI fails on *regression* rather than on absolute
perfection. That distinction is what makes it adoptable — a messy harness
ratchets toward clean instead of blocking every PR on day one.

Seven dimensions, and what each is really asking:

  budget         What does this harness cost on EVERY turn, before any work?
  redundancy     Is any rule stated twice, where one copy can drift?
  references     Does every path and link the harness cites actually exist?
  completeness   Is every artifact fully declared, or partly configured?
  authority      Does anything hold more power than its own description claims?
  executability  Can the commands actually run in the environment they target?
  placement      Is each piece of guidance in the primitive whose job it is?

Determinism is the whole point, and it is a contract rather than an aspiration:

    same tree + same profile + same config + same ruleset version
        -> identical bytes, on any machine, any supported Python, any hash seed.

No network, no clock, no PATH-dependence in the verdict, no LLM. Nothing that
varies between a laptop and CI may reach a finding message, because the message
feeds the fingerprint and the fingerprint backs the ratchet — a fingerprint that
moves between machines makes every run read as drift, which is worse than having
no ratchet at all. Project rules are arbitrary Python and are explicitly outside
this contract. It is a linter, not a reviewer: it measures what is mechanically
checkable and stays quiet about everything else, because a checker that guesses
is one people learn to ignore.

Runner-agnostic by construction. Everything specific to one agent runner — where
its files live, what it calls its tools, how its settings are shaped — lives in a
*profile*, not in the checks. `claude-code`, `agents-md` and `generic` ship; the
profile is detected from the tree, and adding a runner is a data change.

Portability by construction: stdlib only, one file, importing nothing from the
project it measures — so it still runs when the thing it is checking is broken,
which is exactly when you need it. `pip install harnesslint` works; so does
copying this single file into a repo and running it. Config is optional; the
defaults are the opinions.

    harnesslint init            # scaffold a config, a rules dir, a CI snippet
    harnesslint init --scaffold # also stub out the primitives themselves
    harnesslint report          # every finding, never fails the build
    harnesslint baseline        # accept the current state as the ratchet
    harnesslint check           # compare to the baseline (this is what CI runs)
    harnesslint check --strict  # ignore the baseline, fail on any error
    harnesslint explain budget  # what a dimension means and why
    harnesslint profiles        # which runners are known, and what each covers

Vendored instead of installed? Every command above works as
`python3 harnesslint.py …`.

Exit codes: 0 clean or no regression, 1 regression or (under --strict) any
error, 2 a usage or configuration problem. Anything else is a bug in this file.
"""

from __future__ import annotations

import argparse
import ast
import copy
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__version__ = "0.3.0"

RULESET_VERSION = 2
"""Bumped when a rule's *meaning* changes, so a stale baseline is detectable.

Adding a rule does not bump it (new rules land as findings, which the ratchet
already handles). Changing what an existing rule id asserts does — otherwise a
baseline recorded under the old meaning silently suppresses the new one.

v2: `executability/hook-syntax` no longer embeds interpreter or shell output in
its message (that text carried the machine's paths and versions into the
fingerprint); `executability/hook-not-executable` is skipped where the
filesystem cannot represent the bit; `budget/always-loaded` is always reported
against `.` rather than against the settings file.
"""

TOOL_NAME = "harnesslint"

# ── severities ────────────────────────────────────────────────────────────
ERROR = "error"
WARN = "warn"
INFO = "info"
_SEVERITY_ORDER = {ERROR: 0, WARN: 1, INFO: 2}


# ── profiles ──────────────────────────────────────────────────────────────
# A profile is everything one agent runner does differently: where its files
# live, what it calls its tools, how its settings are shaped, which sigil means
# "arguments". Keeping it here rather than inside the checks is what makes the
# seven dimensions genuinely runner-agnostic instead of only claiming to be —
# supporting a new runner is a data change, not a code change.
@dataclass(frozen=True)
class Profile:
    name: str
    summary: str
    # Marker globs. A profile claims a tree when ANY of them match; profiles are
    # probed in declaration order, so the more specific ones come first.
    detect: tuple[str, ...]
    paths: dict[str, list[str]]
    # authority: grants that can change the world. Names are the runner's.
    mutating_tools: list[str]
    # The frontmatter key holding the grant, and how its entries are separated.
    tools_key: str = "tools"
    # completeness: what "this artifact takes arguments" looks like in a body,
    # and the frontmatter key that is then expected to document them.
    arg_sigils: list[str] = field(default_factory=list)
    arg_hint_key: str = ""
    # Frontmatter keys each kind of artifact must declare to be fully declared.
    required_keys: dict[str, list[str]] = field(default_factory=dict)
    # How to walk the settings file: settings[hooks_root][event][i][hooks_entry]
    # is a list of hooks, each with a hooks_command key.
    hooks_root_key: str = ""
    hooks_entry_key: str = "hooks"
    hooks_command_key: str = "command"
    # Key path to the pre-approval allowlist inside the settings file.
    allowlist_path: list[str] = field(default_factory=list)
    # Runner-specific variables a hook command may be written in terms of, which
    # have to come off before the remainder can be resolved against the repo.
    path_vars: list[str] = field(default_factory=list)
    # placement: the runner's own tool names. Empty means "this profile does not
    # know", which switches the check off rather than firing it blindly — a
    # checker guessing at another runner's vocabulary is noise, not measurement.
    known_tools: list[str] = field(default_factory=list)
    # placement: the events a hook may register on. Empty, same reasoning.
    hook_events: list[str] = field(default_factory=list)
    # placement: the frontmatter key carrying a scoped rule's glob. Empty for
    # every runner shipped here, where scope is directory placement instead; the
    # field exists so a runner that declares scope in frontmatter lands as data.
    scope_key: str = ""

    @property
    def inert_dimensions(self) -> list[str]:
        """Dimensions this profile cannot say anything about.

        A profile that quietly checks nothing is the failure this tool exists to
        catch, so the gap is reported rather than left to be discovered.
        """
        out = []
        if not any(self.paths.get(k) for k in ("commands", "agents", "skills")):
            out.append("completeness")
        if not self.paths.get("agents") and not self.paths.get("settings"):
            out.append("authority")
        if not self.paths.get("settings") and not self.paths.get("commands"):
            out.append("executability")
        return out


PROFILES: dict[str, Profile] = {
    "claude-code": Profile(
        name="claude-code",
        summary="Claude Code — .claude/ commands, agents, skills, settings and hooks.",
        detect=(".claude",),
        paths={
            "memory": ["CLAUDE.md", "*/CLAUDE.md", "**/CLAUDE.md", "AGENTS.md"],
            "commands": [".claude/commands/**/*.md"],
            "agents": [".claude/agents/**/*.md"],
            "skills": [".claude/skills/**/SKILL.md"],
            "settings": [".claude/settings.json"],
            "exclude": ["**/node_modules/**", "**/.git/**", "**/vendor/**"],
        },
        mutating_tools=["Edit", "Write", "NotebookEdit", "MultiEdit"],
        tools_key="tools",
        arg_sigils=["$ARGUMENTS", "$1"],
        arg_hint_key="argument-hint",
        required_keys={
            "commands": ["description"],
            "agents": ["name", "description"],
            "skills": ["description"],
        },
        hooks_root_key="hooks",
        allowlist_path=["permissions", "allow"],
        path_vars=["$CLAUDE_PROJECT_DIR/"],
        known_tools=[
            "Bash",
            "BashOutput",
            "Edit",
            "ExitPlanMode",
            "Glob",
            "Grep",
            "KillShell",
            "MultiEdit",
            "NotebookEdit",
            "Read",
            "SlashCommand",
            "Task",
            "TodoWrite",
            "WebFetch",
            "WebSearch",
            "Write",
        ],
        hook_events=[
            "Notification",
            "PostToolUse",
            "PreCompact",
            "PreToolUse",
            "SessionEnd",
            "SessionStart",
            "Stop",
            "SubagentStop",
            "UserPromptSubmit",
        ],
    ),
    "agents-md": Profile(
        name="agents-md",
        summary="The AGENTS.md convention — an instruction file, and no runner-owned layout.",
        detect=("AGENTS.md",),
        paths={
            "memory": ["AGENTS.md", "*/AGENTS.md", "**/AGENTS.md"],
            "commands": [],
            "agents": [],
            "skills": [],
            "settings": [],
            "exclude": ["**/node_modules/**", "**/.git/**", "**/vendor/**"],
        },
        mutating_tools=[],
    ),
    "generic": Profile(
        name="generic",
        summary="No runner detected — instruction files only; runner-specific checks are off.",
        detect=(),
        paths={
            "memory": ["CLAUDE.md", "AGENTS.md", "*/CLAUDE.md", "**/CLAUDE.md"],
            "commands": [],
            "agents": [],
            "skills": [],
            "settings": [],
            "exclude": ["**/node_modules/**", "**/.git/**", "**/vendor/**"],
        },
        mutating_tools=[],
    ),
}

FALLBACK_PROFILE = "generic"


def detect_profile(root: Path) -> Profile:
    """Pick a profile from the tree. Same tree in, same profile out.

    Autodetection is ordered and total, so it costs nothing in determinism: the
    profile is a function of the tree, exactly like the findings are. What it
    buys is that a repo which is not on Claude Code gets an honest answer instead
    of seven dimensions silently measuring globs that match nothing.
    """
    for profile in PROFILES.values():
        if any((root / marker).exists() for marker in profile.detect):
            return profile
    return PROFILES[FALLBACK_PROFILE]


def _profile_overlay(p: Profile) -> dict[str, Any]:
    """The profile, shaped as config so harnesslint.toml can override any of it."""
    return {
        "paths": copy.deepcopy(p.paths),
        "authority": {"mutating_tools": list(p.mutating_tools)},
        "runner": {
            "profile": p.name,
            "tools_key": p.tools_key,
            "arg_sigils": list(p.arg_sigils),
            "arg_hint_key": p.arg_hint_key,
            "required_keys": copy.deepcopy(p.required_keys),
            "hooks_root_key": p.hooks_root_key,
            "hooks_entry_key": p.hooks_entry_key,
            "hooks_command_key": p.hooks_command_key,
            "allowlist_path": list(p.allowlist_path),
            "path_vars": list(p.path_vars),
            "known_tools": list(p.known_tools),
            "hook_events": list(p.hook_events),
            "scope_key": p.scope_key,
        },
    }


# ── configuration ─────────────────────────────────────────────────────────
# The runner-neutral half of the opinions. Anything that differs between agent
# runners lives in a Profile above; everything here applies to any harness.
# Every value is overridable in harnesslint.toml.
DEFAULTS: dict[str, Any] = {
    "budget": {
        # Cost of what loads on EVERY turn: root memory + the description line
        # of every command, agent and skill (those are listed to the model
        # whether or not they are invoked).
        "always_loaded_tokens": 24000,
        # Root memory alone. A memory file past this stops being read closely.
        "memory_file_tokens": 12000,
        # chars/token. A deliberate approximation — see estimate_tokens().
        "chars_per_token": 4,
        # A description long enough to be a paragraph is a description nobody
        # skims; it is also paid for on every single turn.
        "max_description_chars": 700,
    },
    "redundancy": {
        # A sentence this long appearing verbatim in two harness files is a
        # rule with two homes. One of them will drift.
        "min_words": 9,
    },
    "authority": {
        # Pre-approving any of these silently authorises publishing.
        "outward_facing": [
            "git push",
            "gh pr create",
            "gh issue create",
            "gh issue comment",
            "gh release",
            "npm publish",
            "railway",
            "kubectl",
            "terraform apply",
            "aws ",
        ],
        # Phrases by which an agent claims to be read-only. Holding a mutating
        # tool while claiming one of these is a contradiction, not a nuance.
        "read_only_claims": [
            "read-only",
            "read only",
            "never edits",
            "never deploys",
            "never merges",
            "you report",
            "a human ships",
        ],
        # Filled in by the profile — tool names belong to the runner.
        "mutating_tools": [],
    },
    "executability": {
        # CLIs a harness may assume without declaring. Anything else invoked in
        # a fenced block must be listed in `declared_clis`, so an environment
        # that lacks it is a known gap rather than a surprise mid-task.
        "assumed_clis": ["git", "python3", "python", "bash", "sh", "echo", "cd"],
        "declared_clis": [],
        # Off by default, and that is a determinism decision rather than a
        # timidity one: `bash -n` makes the verdict depend on what happens to be
        # installed, which is precisely what this tool promises not to do. Turn
        # it on where bash is guaranteed; when it is off, the fact that shell
        # hooks went unchecked is reported rather than left silent.
        "shell_syntax_check": False,
        # "auto" skips the check where the filesystem cannot carry the bit
        # (Windows checkouts, core.fileMode=false, zip exports) instead of
        # reporting every hook as broken. "on" and "off" force the question.
        "exec_bit_check": "auto",
    },
    "placement": {
        # Consecutive numbered steps in the always-loaded file. Past this it is
        # a procedure, and a procedure paid for on every turn is read closely on
        # none — it belongs in a skill, where invocation is what costs.
        "max_memory_steps": 6,
        # An agent body this long stopped being a bounded task and became a
        # procedure being run as one. That is a skill.
        "max_agent_tokens": 1500,
        # Tool names this project has that the profile cannot know about —
        # plugin-provided tools, mostly. The escape hatch for
        # placement/agent-tool-unknown, so a real grant is never called a typo.
        "extra_tools": [],
        # What counts as an instruction file rather than as content, when asking
        # whether a scoped rule governs anything at all.
        "instruction_file_names": ["AGENTS.md", "CLAUDE.md"],
    },
    "runner": {},  # filled in by the profile; see _profile_overlay
    "severities": {},  # rule id -> severity override
    "ignore": [],  # rule ids or "rule:path" pairs to skip entirely
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(root: Path) -> dict[str, Any]:
    """Resolve the effective config. Absent config is a valid state.

    Three layers, in order, each overriding the last:

        DEFAULTS          the runner-neutral opinions
        profile overlay   whatever this runner does differently
        harnesslint.toml  whatever THIS repo decided differently

    The profile goes in the middle so that a repo can override any part of it —
    a `[paths]` block in the TOML still wins, exactly as it did before profiles
    existed.
    """
    user: dict[str, Any] = {}
    path = root / f"{TOOL_NAME}.toml"
    if path.is_file():
        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover - 3.10 and older
            die(
                f"{path.name} found but this Python has no tomllib (needs 3.11+). "
                "Delete the config to run on defaults, or upgrade Python."
            )
        with path.open("rb") as fh:
            try:
                user = tomllib.load(fh)
            except tomllib.TOMLDecodeError as exc:
                die(f"{path.name} is not valid TOML: {exc}")

    requested = (user.get("runner") or {}).get("profile")
    if requested is not None and requested not in PROFILES:
        die(
            f"unknown runner profile {requested!r}. "
            f"Known: {', '.join(PROFILES)}. Run `{TOOL_NAME} profiles` to see what each covers."
        )
    profile = PROFILES[requested] if requested else detect_profile(root)

    config = _deep_merge(copy.deepcopy(DEFAULTS), _profile_overlay(profile))
    return _deep_merge(config, user)


# ── findings ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Finding:
    dimension: str
    rule: str
    severity: str
    path: str
    message: str
    line: int = 0

    @property
    def fingerprint(self) -> str:
        """Stable id for the ratchet.

        Deliberately excludes the line number and the message's variable parts:
        reflowing a paragraph or renumbering a file must not read as a new
        finding, or the baseline churns on every cosmetic edit and stops being
        trusted. Rule + path + the salient token is enough to identify "this
        problem, in this file".
        """
        salient = _SALIENT.sub("", self.message).strip()
        raw = f"{self.rule}|{self.path}|{salient}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]


# Numbers and quoted spans vary run to run (counts, sizes, line refs) without
# changing which problem is being reported.
_SALIENT = re.compile(r"\d+|`[^`]*`|\"[^\"]*\"")


@dataclass
class Doc:
    """A harness markdown file: frontmatter plus body."""

    path: Path
    rel: str
    frontmatter: dict[str, str]
    body: str
    raw: str


@dataclass
class Harness:
    root: Path
    config: dict[str, Any]
    memory: list[Doc] = field(default_factory=list)
    commands: list[Doc] = field(default_factory=list)
    agents: list[Doc] = field(default_factory=list)
    skills: list[Doc] = field(default_factory=list)
    settings: dict[str, Any] | None = None
    settings_path: Path | None = None
    # Set when a settings file exists but could not be parsed. Distinguishing
    # this from "no settings file" is the difference between reporting a problem
    # and silently checking nothing.
    settings_error: str | None = None

    @property
    def all_docs(self) -> list[Doc]:
        return [*self.memory, *self.commands, *self.agents, *self.skills]

    @property
    def profile(self) -> str:
        return (self.config.get("runner") or {}).get("profile", FALLBACK_PROFILE)

    @property
    def settings_rel(self) -> str:
        if self.settings_path:
            return self.settings_path.relative_to(self.root).as_posix()
        return "settings.json"


# ── discovery ─────────────────────────────────────────────────────────────
_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.S)


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse the leading `---` block.

    A deliberately minimal YAML subset — flat `key: value` only, which is all
    an agent-runner frontmatter uses. Taking a YAML dependency to read six keys
    would cost this file its "copy it anywhere" property, which is worth more
    than handling nested frontmatter nobody writes.
    """
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text
    data: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        data[key.strip()] = value.strip().strip("'\"")
    return data, text[m.end() :]


def _iter_globs(root: Path, patterns: Iterable[str], exclude: Iterable[str]) -> list[Path]:
    seen: dict[Path, None] = {}
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if any(fnmatch.fnmatch(rel, ex) for ex in exclude):
                continue
            seen.setdefault(path, None)
    return list(seen)


def _load_doc(root: Path, path: Path) -> Doc:
    raw = path.read_text(encoding="utf-8", errors="replace")
    fm, body = parse_frontmatter(raw)
    return Doc(
        path=path,
        rel=path.relative_to(root).as_posix(),
        frontmatter=fm,
        body=body,
        raw=raw,
    )


def discover(root: Path, config: dict[str, Any]) -> Harness:
    paths = config["paths"]
    exclude = paths.get("exclude", [])
    h = Harness(root=root, config=config)
    for kind in ("memory", "commands", "agents", "skills"):
        docs = [_load_doc(root, p) for p in _iter_globs(root, paths.get(kind, []), exclude)]
        setattr(h, kind, sorted(docs, key=lambda d: d.rel))
    for candidate in _iter_globs(root, paths.get("settings", []), exclude):
        h.settings_path = candidate
        try:
            h.settings = json.loads(candidate.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # Deliberately not silent. Swallowing this is how the checker itself
            # would fail open: an unparseable settings file switches off the
            # allowlist and hook checks, and a clean report would then mean
            # "nothing was looked at" while reading as "nothing is wrong".
            h.settings = None
            h.settings_error = type(exc).__name__
        break
    return h


def _dig(obj: Any, path: Iterable[str]) -> Any:
    """Follow a key path through nested dicts, or return None."""
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


# ── shared text helpers ───────────────────────────────────────────────────
_FENCE = re.compile(r"```([^\n]*)\n(.*?)```", re.S)
_INLINE_CODE = re.compile(r"`[^`\n]+`")
_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def strip_code(text: str) -> str:
    return _INLINE_CODE.sub(" ", _FENCE.sub(" ", text))


def fenced_blocks(text: str) -> list[tuple[str, str]]:
    """(language tag, body) for every fenced block. The tag decides how to read
    the body — a ```bash block holds commands, a ```python one does not."""
    return [(lang.strip(), body) for lang, body in _FENCE.findall(text)]


def estimate_tokens(text: str, chars_per_token: int) -> int:
    """Deterministic token estimate.

    chars/N, not a real tokenizer. A real one means a dependency and a model
    choice, and both would make this file unportable for a number whose job is
    to be compared against its own previous value. It is a ratchet, not a bill —
    the trend is what matters, and the divisor is configurable if your content
    tokenizes unusually.

    Line endings are normalised first, so the same file checked out with CRLF
    and with LF costs the same. Otherwise a Windows clone reads as a budget
    regression against a baseline recorded on Linux, which is drift invented by
    the tool rather than found by it.
    """
    normalised = text.replace("\r\n", "\n")
    return (len(normalised) + chars_per_token - 1) // max(1, chars_per_token)


# ── dimension: budget ─────────────────────────────────────────────────────
def check_budget(h: Harness) -> list[Finding]:
    """What the harness costs before any work happens.

    Two populations, and conflating them is the usual mistake. Root memory and
    every artifact's `description:` load on EVERY turn — a command's description
    is paid for whether or not it is ever invoked. A command's *body* is not: it
    loads when invoked, so a long, careful procedure is cheap and a long
    description is not.
    """
    cfg = h.config["budget"]
    cpt = cfg["chars_per_token"]
    out: list[Finding] = []

    root_memory = [d for d in h.memory if d.rel.count("/") == 0]
    scoped_memory = [d for d in h.memory if d.rel.count("/") > 0]

    always = 0
    for doc in root_memory:
        tokens = estimate_tokens(doc.raw, cpt)
        always += tokens
        if tokens > cfg["memory_file_tokens"]:
            out.append(
                Finding(
                    "budget",
                    "budget/memory-file",
                    WARN,
                    doc.rel,
                    f"root memory file is ~{tokens} est. tokens "
                    f"(budget {cfg['memory_file_tokens']}); it loads on every turn, "
                    "and past this size it stops being read closely — move detail "
                    "behind a link or into a scoped file",
                )
            )

    described = [*h.commands, *h.agents, *h.skills]
    for doc in described:
        desc = doc.frontmatter.get("description", "")
        always += estimate_tokens(desc, cpt)
        if len(desc) > cfg["max_description_chars"]:
            out.append(
                Finding(
                    "budget",
                    "budget/description-length",
                    WARN,
                    doc.rel,
                    f"description is {len(desc)} chars "
                    f"(max {cfg['max_description_chars']}); it is listed to the model "
                    "on every turn whether or not this is ever invoked",
                )
            )

    if always > cfg["always_loaded_tokens"]:
        out.append(
            Finding(
                "budget",
                "budget/always-loaded",
                ERROR,
                # Always `.`, never the settings file. This is a fact about the
                # whole harness, and pinning it to a file that may or may not
                # exist moved its fingerprint for reasons unrelated to budget.
                ".",
                f"harness costs ~{always} est. tokens on every turn "
                f"(budget {cfg['always_loaded_tokens']}) across "
                f"{len(root_memory)} memory file(s) and {len(described)} description(s)",
            )
        )

    # Not a failure — a fact worth surfacing, because scoped memory is how you
    # get the always-loaded number DOWN without losing the rule.
    if scoped_memory:
        scoped_tokens = sum(estimate_tokens(d.raw, cpt) for d in scoped_memory)
        out.append(
            Finding(
                "budget",
                "budget/scoped-memory",
                INFO,
                ".",
                f"{len(scoped_memory)} scoped memory file(s), ~{scoped_tokens} est. tokens, "
                "loaded only on contact — this is the lever for reducing always-loaded cost",
            )
        )
    return out


# ── dimension: redundancy ─────────────────────────────────────────────────
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n{2,}|\n[-*] ")
_NORMALISE = re.compile(r"[^a-z0-9 ]+")


def _sentences(text: str, min_words: int) -> set[str]:
    out = set()
    for chunk in _SENTENCE_SPLIT.split(strip_code(text)):
        norm = _NORMALISE.sub(" ", chunk.lower())
        words = norm.split()
        if len(words) >= min_words:
            out.add(" ".join(words))
    return out


def check_redundancy(h: Harness) -> list[Finding]:
    """A rule with two homes has one that will drift.

    When the same sentence appears in two harness files, both are now
    maintained, but only one will be edited when the behaviour changes — the
    original. The copy is always the one that goes stale, and it goes stale
    silently, still reading as authoritative.

    State the rule once and link to it. The threshold is a sentence length,
    because that is what separates a restated rule from shared boilerplate.
    """
    cfg = h.config["redundancy"]
    index: dict[str, list[str]] = {}
    for doc in h.all_docs:
        for sentence in _sentences(doc.body, cfg["min_words"]):
            index.setdefault(sentence, []).append(doc.rel)

    out: list[Finding] = []
    for sentence, files in sorted(index.items()):
        homes = sorted(set(files))
        if len(homes) < 2:
            continue
        preview = sentence[:70] + ("…" if len(sentence) > 70 else "")
        out.append(
            Finding(
                "redundancy",
                "redundancy/duplicate-rule",
                WARN,
                homes[0],
                f'"{preview}" also appears in {", ".join(homes[1:])} — '
                "state it once and link to it; the copy is the one that drifts",
            )
        )
    return out


# ── dimension: references ─────────────────────────────────────────────────
_PATHISH = re.compile(r"`([A-Za-z0-9_./-]+\.(?:md|py|sh|ya?ml|json|toml|txt))`")


def check_references(h: Harness) -> list[Finding]:
    """Every path the harness cites must exist.

    A harness that points at a moved file teaches the agent the file is
    optional. Broken references are also the cheapest possible signal that a
    refactor forgot the docs.
    """
    out: list[Finding] = []
    for doc in h.all_docs:
        base = doc.path.parent
        targets: list[tuple[str, bool]] = []
        for link in _MD_LINK.findall(doc.raw):
            if link.startswith(("http://", "https://", "#", "mailto:")):
                continue
            targets.append((link.split("#")[0], True))
        for path in _PATHISH.findall(doc.raw):
            targets.append((path, False))

        for target, is_link in sorted(set(targets)):
            if not target:
                continue
            # A glob or a `<placeholder>` is a pattern, not a reference.
            if any(ch in target for ch in "*<>$"):
                continue
            if any(c.exists() for c in (base / target, h.root / target)):
                continue

            # Before calling it broken, find out whether the file simply lives
            # somewhere else. "Cites `deploy.sh`, which does not exist" is
            # false when `scripts/deploy.sh` is right there — and a checker
            # that says false things gets ignored. Three different problems
            # hide behind one unresolved citation, and each has its own fix.
            elsewhere = _find_by_basename(h, target)

            if len(elsewhere) == 1:
                out.append(
                    Finding(
                        "references",
                        "references/unqualified-path",
                        WARN,
                        doc.rel,
                        f"cites `{target}` by bare filename; it lives at "
                        f"`{elsewhere[0]}` — cite the path so the reference "
                        "survives the next move",
                    )
                )
            elif len(elsewhere) > 1:
                out.append(
                    Finding(
                        "references",
                        "references/ambiguous-path",
                        WARN,
                        doc.rel,
                        f"cites `{target}` by bare filename, but {len(elsewhere)} files "
                        f"share that name ({', '.join(elsewhere)}) — a reader cannot "
                        "tell which one is meant",
                    )
                )
            else:
                out.append(
                    Finding(
                        "references",
                        "references/broken-link" if is_link else "references/missing-path",
                        ERROR if is_link else WARN,
                        doc.rel,
                        f"cites `{target}`, which exists nowhere in the repo",
                    )
                )
    return out


def _find_by_basename(h: Harness, target: str) -> list[str]:
    """Every file in the repo sharing this bare filename.

    Returns nothing for a target that is already a path — that one is genuinely
    missing, and searching for its basename would only muddy the message.
    """
    name = Path(target).name
    if name != target:
        return []
    exclude = h.config["paths"].get("exclude", [])
    return sorted(
        p.relative_to(h.root).as_posix()
        for p in h.root.rglob(name)
        if p.is_file()
        and ".git/" not in p.relative_to(h.root).as_posix()
        and not any(fnmatch.fnmatch(p.relative_to(h.root).as_posix(), ex) for ex in exclude)
    )


# ── dimension: completeness ───────────────────────────────────────────────
def check_completeness(h: Harness) -> list[Finding]:
    """Half-declared artifacts are the ones that behave unpredictably.

    A command with no description is invisible in the listing. An agent with no
    `tools:` inherits everything. A hook registered but absent fails open, which
    is the single worst outcome — the gate reports nothing and blocks nothing.
    """
    runner = h.config["runner"]
    required = runner.get("required_keys", {})
    out: list[Finding] = []

    if h.settings_error:
        out.append(
            Finding(
                "completeness",
                "completeness/settings-unparseable",
                ERROR,
                h.settings_rel,
                "settings file does not parse — the allowlist and hook checks cannot run "
                "against it, so a clean report here means nothing was read, not that "
                "nothing is wrong",
            )
        )

    for doc in h.commands:
        if "description" in required.get("commands", []) and not doc.frontmatter.get("description"):
            out.append(
                Finding(
                    "completeness",
                    "completeness/command-description",
                    ERROR,
                    doc.rel,
                    "command has no `description:` — it cannot be chosen from the listing",
                )
            )
        sigils = runner.get("arg_sigils", [])
        hint_key = runner.get("arg_hint_key", "")
        takes_args = any(sigil in doc.raw for sigil in sigils)
        if takes_args and hint_key and not doc.frontmatter.get(hint_key):
            out.append(
                Finding(
                    "completeness",
                    "completeness/argument-hint",
                    WARN,
                    doc.rel,
                    f"command reads `{sigils[0]}` but declares no `{hint_key}:`",
                )
            )

    for doc in h.agents:
        for key in required.get("agents", []):
            if not doc.frontmatter.get(key):
                out.append(
                    Finding(
                        "completeness",
                        "completeness/agent-frontmatter",
                        ERROR,
                        doc.rel,
                        f"agent has no `{key}:`",
                    )
                )

    for doc in h.skills:
        if "description" in required.get("skills", []) and not doc.frontmatter.get("description"):
            out.append(
                Finding(
                    "completeness",
                    "completeness/skill-description",
                    ERROR,
                    doc.rel,
                    "skill has no `description:` — it will never trigger",
                )
            )

    for ref, rel in _registered_hooks(h):
        if not ref.exists():
            out.append(
                Finding(
                    "completeness",
                    "completeness/hook-missing",
                    ERROR,
                    rel,
                    f"settings registers `{ref.name}`, which does not exist — "
                    "a missing hook fails OPEN and reports nothing",
                )
            )
    return out


def _registered_hooks(h: Harness) -> list[tuple[Path, str]]:
    """Resolve every hook command in settings to a path on disk.

    Only file-shaped commands resolve; an inline shell one-liner is a valid
    hook and simply has no path to check.
    """
    runner = h.config["runner"]
    root_key = runner.get("hooks_root_key", "")
    if not h.settings or not root_key:
        return []
    entry_key = runner.get("hooks_entry_key", "hooks")
    command_key = runner.get("hooks_command_key", "command")
    out: list[tuple[Path, str]] = []
    for entries in (h.settings.get(root_key) or {}).values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            for hook in (entry or {}).get(entry_key, []) or []:
                command = (hook or {}).get(command_key, "")
                if not isinstance(command, str) or not command.split():
                    continue
                token = command.split()[0]
                for var in runner.get("path_vars", []):
                    token = token.replace(var, "")
                if token.startswith("~/"):
                    continue  # a home-dir hook is outside the repo; nothing to resolve
                if "/" not in token or not Path(token).suffix:
                    continue
                out.append((h.root / token, h.settings_rel))
    return out


# ── dimension: authority ──────────────────────────────────────────────────
def check_authority(h: Harness) -> list[Finding]:
    """Does anything hold more power than its own description claims?

    Two failure shapes. An agent that says "read-only" while holding `Write` is
    a contradiction a reader will resolve in favour of the prose and be wrong.
    An allowlist that pre-approves an outward-facing command has quietly
    converted "ask me first" into "publish freely" — and nobody re-reads an
    allowlist once it works.
    """
    cfg = h.config["authority"]
    runner = h.config["runner"]
    tools_key = runner.get("tools_key", "tools")
    out: list[Finding] = []

    for doc in h.agents:
        tools = doc.frontmatter.get(tools_key, "")
        granted = {t.strip() for t in tools.split(",") if t.strip()}
        blurb = (doc.frontmatter.get("description", "") + " " + doc.body[:2000]).lower()
        claims_ro = any(c in blurb for c in cfg["read_only_claims"])

        if not tools:
            out.append(
                Finding(
                    "authority",
                    "authority/agent-inherits-all",
                    WARN,
                    doc.rel,
                    f"agent declares no `{tools_key}:` and inherits every tool — "
                    "grant the narrowest set that does the job",
                )
            )
        elif claims_ro:
            held = sorted(granted & set(cfg["mutating_tools"]))
            if held:
                out.append(
                    Finding(
                        "authority",
                        "authority/read-only-contradiction",
                        ERROR,
                        doc.rel,
                        f"describes itself as read-only but holds {', '.join(held)} — "
                        "the prose and the grant disagree, and only one is enforced",
                    )
                )

    allowlist_path = runner.get("allowlist_path", [])
    allow = _dig(h.settings or {}, allowlist_path) if allowlist_path else None
    for entry in allow or []:
        if not isinstance(entry, str):
            continue
        for pattern in cfg["outward_facing"]:
            if pattern in entry:
                out.append(
                    Finding(
                        "authority",
                        "authority/outward-facing-preapproved",
                        ERROR,
                        h.settings_rel,
                        f"`{entry}` pre-approves an outward-facing action "
                        f"(`{pattern.strip()}`) — publishing should stay behind a prompt",
                    )
                )
    return out


# ── dimension: executability ──────────────────────────────────────────────
_SHELL_LANGS = {"bash", "sh", "shell", "zsh", "console", "shell-session"}

# Shell grammar, not programs. A first draft of this check flagged `for`,
# `done`, `import` and `print` as undeclared CLIs — 49 findings, nearly all
# noise. A checker that cries wolf is one people learn to ignore, which is the
# failure it exists to prevent, so the exclusions below are load-bearing.
_SHELL_WORDS = frozenset(
    [
        "if",
        "then",
        "else",
        "elif",
        "fi",
        "for",
        "while",
        "until",
        "do",
        "done",
        "case",
        "esac",
        "in",
        "select",
        "function",
        "time",
        "coproc",
        "return",
        "exit",
        "break",
        "continue",
        "local",
        "export",
        "unset",
        "set",
        "shift",
        "declare",
        "readonly",
        "typeset",
        "let",
        "eval",
        "exec",
        "source",
        "trap",
        "wait",
        "alias",
        "unalias",
        "getopts",
        "read",
        "printf",
        "echo",
        "test",
        "true",
        "false",
        "pwd",
        "command",
        "builtin",
        "type",
        "hash",
        "umask",
        "ulimit",
        "jobs",
        "fg",
        "bg",
        "kill",
        "disown",
        "enable",
        "mapfile",
        "readarray",
        "shopt",
        "caller",
        "compgen",
        "complete",
    ]
)

_HEREDOC_OPEN = re.compile(r"<<-?\s*[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?")
# `VAR=$(` opens a command substitution — the command is what follows.
# `VAR=value ` is an env prefix — the command is what follows the space.
_ASSIGN_SUB = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*=\$\(\s*")
_ASSIGN_ENV = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*=[^\s$]*\s+")
# A command is the first bare word on a line, optionally after a `$ ` prompt.
_CLI_CALL = re.compile(r"^\s*(?:\$\s+)?([a-z][a-z0-9_.-]{1,30})(?=\s|$)")


def _shell_commands(block: str) -> set[str]:
    """First token of every shell command in a fenced block.

    Three things make this quiet enough to be worth running, each learned from
    a false positive on this repo's own harness:

    * **Heredoc bodies are skipped.** `python3 - <<'PY' … PY` is Python, and
      reading its identifiers as binaries reported `import` and `print` as
      undeclared CLIs.
    * **Multi-line quoted strings are skipped.** `python -c "` followed by four
      lines of Python, and `gh api -f query='{` followed by GraphQL, are both
      one command whose continuation lines are data.
    * **`VAR=$(cmd …)` resolves to `cmd`,** not to its second word. Without
      this, `ITEM=$(gh project item-list …)` reported `project` as a CLI.

    Quote tracking is a parity count, not a shell parser. When it desyncs it
    errs toward skipping lines — the safe direction for a check whose whole
    problem is noise.
    """
    found: set[str] = set()
    terminator: str | None = None
    in_quote: str | None = None

    for line in block.splitlines():
        if terminator is not None:  # inside a heredoc body
            if line.strip() == terminator:
                terminator = None
            continue

        stripped_line = line
        if in_quote is None:
            # Peel any assignment prefixes, innermost first.
            while True:
                new = _ASSIGN_SUB.sub("", stripped_line, count=1)
                if new == stripped_line:
                    new = _ASSIGN_ENV.sub("", stripped_line, count=1)
                if new == stripped_line:
                    break
                stripped_line = new
            m = _CLI_CALL.match(stripped_line)
            if m and m.group(1) not in _SHELL_WORDS:
                found.add(m.group(1))
            opener = _HEREDOC_OPEN.search(line)
            if opener:
                terminator = opener.group(1)
                continue

        for quote in ("'", '"'):
            if in_quote in (None, quote) and _unescaped_count(line, quote) % 2:
                in_quote = None if in_quote == quote else quote
    return found


def _unescaped_count(line: str, quote: str) -> int:
    return len(re.findall(r"(?<!\\)" + re.escape(quote), line))


def check_executability(h: Harness) -> list[Finding]:
    """Can these commands run where they are aimed?

    The failure this catches is a harness built on a CLI that is absent from one
    of the environments it runs in — a cloud session, a fresh CI container, a
    new laptop. Every command that depends on it fails at its first step, in an
    environment the team may use daily.

    The objection is not to depending on a CLI. It is to depending on one
    *silently*. Declaring it turns "this breaks for reasons nobody understands"
    into a stated precondition each command can be written to handle.

    The hook checks here follow the same principle applied to this tool itself:
    where a check cannot be run without making the verdict depend on what is
    installed, the fact that it was skipped is reported rather than assumed.
    """
    cfg = h.config["executability"]
    known = set(cfg["assumed_clis"]) | set(cfg["declared_clis"])
    out: list[Finding] = []

    for doc in h.all_docs:
        undeclared: set[str] = set()
        for lang, block in fenced_blocks(doc.raw):
            if lang.lower() not in _SHELL_LANGS:
                continue  # only shell blocks contain shell commands
            undeclared |= _shell_commands(block) - known
        for cli in sorted(undeclared):
            out.append(
                Finding(
                    "executability",
                    "executability/undeclared-cli",
                    WARN,
                    doc.rel,
                    f"invokes `{cli}` but it is not in `executability.declared_clis` — "
                    "declare it so an environment without it is a known precondition, "
                    "not a mid-task surprise",
                )
            )

    shell_check = bool(cfg.get("shell_syntax_check", False))
    exec_bit_mode = cfg.get("exec_bit_check", "auto")
    exec_bit_readable = (
        _exec_bit_is_meaningful() if exec_bit_mode == "auto" else (exec_bit_mode == "on")
    )
    unchecked_shell: list[str] = []

    for ref, _rel in _registered_hooks(h):
        if not ref.exists():
            continue  # completeness already reported it
        rel = ref.relative_to(h.root).as_posix()
        if ref.suffix == ".py":
            line = _python_syntax_error_line(ref)
            if line is not None:
                out.append(
                    Finding(
                        "executability",
                        "executability/hook-syntax",
                        ERROR,
                        rel,
                        # Never the interpreter's own message. SyntaxError
                        # wording is a CPython implementation detail that has
                        # been reworded across releases, and embedding it would
                        # make this finding's fingerprint depend on which Python
                        # ran the check rather than on the repo.
                        f"does not parse (syntax error at line {line})",
                    )
                )
        elif ref.suffix in {".sh", ".bash", ""}:
            if not shell_check:
                unchecked_shell.append(rel)
            else:
                verdict, line = _shell_syntax_error_line(h.root, rel)
                if verdict == "unavailable":
                    out.append(
                        Finding(
                            "executability",
                            "executability/hook-uncheckable",
                            INFO,
                            rel,
                            "shell syntax check is enabled but `bash` could not be run here — "
                            "this hook was not checked",
                        )
                    )
                elif verdict == "invalid":
                    where = f" at line {line}" if line else ""
                    out.append(
                        Finding(
                            "executability",
                            "executability/hook-syntax",
                            ERROR,
                            rel,
                            f"does not parse (shell syntax error{where})",
                        )
                    )
        if exec_bit_readable and not ref.stat().st_mode & 0o111:
            out.append(
                Finding(
                    "executability",
                    "executability/hook-not-executable",
                    ERROR,
                    rel,
                    "registered as a hook but is not executable — it will fail OPEN",
                )
            )

    if unchecked_shell:
        out.append(
            Finding(
                "executability",
                "executability/shell-syntax-unchecked",
                INFO,
                ".",
                f"{len(unchecked_shell)} shell hook(s) were not syntax-checked; "
                "`bash -n` is off by default because a verdict that depends on what is "
                "installed is not reproducible — set `executability.shell_syntax_check` "
                "where bash is guaranteed",
            )
        )
    if not exec_bit_readable and _registered_hooks(h):
        out.append(
            Finding(
                "executability",
                "executability/exec-bit-unverifiable",
                INFO,
                ".",
                "this filesystem does not carry the executable bit, so hooks were not "
                "checked for it — a hook that is not executable fails OPEN, and that "
                "cannot be confirmed here",
            )
        )
    return out


def _python_syntax_error_line(ref: Path) -> int | None:
    """The line a Python hook fails to parse on, or None if it parses."""
    try:
        ast.parse(ref.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError as exc:
        return exc.lineno or 0
    except ValueError:
        # Source with null bytes and similar: unparseable, location unknown.
        return 0
    return None


_BASH_ERROR_LINE = re.compile(r"line (\d+)")


def _shell_syntax_error_line(root: Path, rel: str) -> tuple[str, int | None]:
    """('ok'|'invalid'|'unavailable', line). Runs bash from the repo root.

    The path handed to bash is repo-relative and the process runs with cwd at
    the root, so nothing machine-specific can appear in what comes back — the
    absolute path of the checkout used to end up inside the finding message, and
    from there inside the fingerprint.
    """
    try:
        proc = subprocess.run(
            ["bash", "-n", "--", rel],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=root,
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable", None
    if proc.returncode == 0:
        return "ok", None
    match = _BASH_ERROR_LINE.search(proc.stderr or "")
    return "invalid", int(match.group(1)) if match else None


def _exec_bit_is_meaningful() -> bool:
    """Whether this platform's filesystem records an executable bit.

    Windows checkouts, `core.fileMode=false` clones and zip exports all report
    every file as non-executable. Reporting each hook as broken there would be a
    checker crying wolf about the platform rather than about the harness.
    """
    return os.name == "posix"


# ── dimension: placement ──────────────────────────────────────────────────
# Markdown structure only. Every trigger below is an ordered-list item, a
# heading, a filesystem fact or a key lookup — never a phrase in prose, because
# a rule that has to read intent is a rule that guesses, and this file does not.
_ORDERED_ITEM = re.compile(r"^ {0,3}\d+[.)]\s+\S")
_STEP_HEADING = re.compile(r"^#+\s*step\b", re.I)


def _longest_step_run(body: str) -> int:
    """The longest run of consecutive ordered-list items in a body.

    Blank and indented lines continue a run rather than ending it — a list whose
    items carry a second paragraph is still one list, and splitting it into
    several short runs would let a twelve-step procedure pass as six two-step
    ones. Called on `strip_code` output, so a numbered list inside a fenced
    example never counts: showing a procedure is not stating one.
    """
    longest = run = 0
    for line in body.splitlines():
        if _ORDERED_ITEM.match(line):
            run += 1
            longest = max(longest, run)
        elif not line.strip() or line[:1] in (" ", "\t"):
            continue
        else:
            run = 0
    return longest


def _governs_something(directory: Path, instruction_names: set[str]) -> bool:
    """Whether a directory holds anything a rule placed in it could govern.

    Deliberately generous: a subdirectory counts, because scope is a subtree and
    a rule over `docs/` is not dead just because every file sits one level down.
    What it takes to fail is a directory holding instruction files and nothing
    else — a rule with nothing under it, which is a rule that stopped applying
    without anything saying so.
    """
    try:
        entries = sorted(directory.iterdir(), key=lambda e: e.name)
    except OSError:
        return True  # unreadable is unknown, and unknown is not a finding
    for entry in entries:
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            return True
        if entry.name not in instruction_names:
            return True
    return False


def check_placement(h: Harness) -> list[Finding]:
    """Is each piece of guidance living in the primitive whose job it is?

    Five primitives, and each has one job:

      instructions file  The handful of facts true for EVERY task — build
                         command, test command, the one architectural rule
                         everyone gets wrong. It is paid for on every turn, so
                         length is the whole cost.
      scoped rules       Conventions for part of the tree, loaded on contact.
                         This is how the always-loaded file stays small without
                         losing the guidance.
      skills             A procedure with steps, invoked when a kind of task
                         comes up. Invocation is what it costs.
      subagents          A bounded task whose exploration is large and whose
                         answer is small. The isolation is the point.
      hooks              Anything that MUST happen, or must not.

    Misplacement is the same fail-open shape as the rest of this tool. A
    procedure in the instructions file is paid for on every turn and read
    closely on none. A skill that is really a rule applies only when someone
    thinks to invoke it. A scoped file over a directory that no longer holds
    anything is a rule that silently stopped applying. A hook on a misspelled
    event never fires and never says so.

    Hard-mechanical only: markdown structure, a filesystem fact, or a key
    lookup. Three attractive rules do not clear that bar and are therefore not
    here — an obligation stated in prose that no hook enforces, a subagent with
    no return contract, and a rule in the root file that ought to be scoped.
    Each needs intent read out of prose. `no-hooks-registered` covers the
    honest, countable half of the first: this harness enforces nothing.

    Narrower than coherence on purpose. Placement asks whether guidance is in
    the right KIND of file; coherence asks whether two pieces of guidance agree.
    """
    cfg = h.config["placement"]
    runner = h.config["runner"]
    paths = h.config["paths"]
    tools_key = runner.get("tools_key", "tools")
    instruction_names = set(cfg["instruction_file_names"])
    out: list[Finding] = []

    # ── the project instructions file ─────────────────────────────────────
    root_memory = [d for d in h.memory if "/" not in d.rel]
    has_artifacts = bool(h.commands or h.agents or h.skills or h.settings_path)
    if has_artifacts and not root_memory:
        out.append(
            Finding(
                "placement",
                "placement/no-instruction-file",
                ERROR,
                ".",
                "harness artifacts exist but no root instruction file does — "
                "commands, agents and hooks are steering a runner that was given "
                "no baseline to steer from",
            )
        )

    max_steps = int(cfg["max_memory_steps"])
    for doc in root_memory:
        steps = _longest_step_run(strip_code(doc.body))
        if steps >= max_steps:
            out.append(
                Finding(
                    "placement",
                    "placement/procedure-in-memory",
                    WARN,
                    doc.rel,
                    f"{steps} consecutive numbered steps in the always-loaded file — "
                    "a procedure belongs in a skill, where it is paid for on "
                    "invocation instead of on every turn",
                )
            )

    # ── scoped rules ──────────────────────────────────────────────────────
    scope_key = runner.get("scope_key", "")
    for doc in h.memory:
        if doc.rel.count("/") > 0 and not _governs_something(doc.path.parent, instruction_names):
            out.append(
                Finding(
                    "placement",
                    "placement/scope-matches-nothing",
                    ERROR,
                    doc.rel,
                    "scoped instructions over a directory holding nothing else — "
                    "the rule governs no file, and a rule that stopped applying "
                    "does not announce it",
                )
            )
        if scope_key and scope_key not in doc.frontmatter:
            out.append(
                Finding(
                    "placement",
                    "placement/scope-not-declared",
                    WARN,
                    doc.rel,
                    f"no `{scope_key}:` in frontmatter — under this runner scope is "
                    "declared, not inferred from where the file sits, so an "
                    "undeclared rule applies somewhere nobody chose",
                )
            )

    # ── skills ────────────────────────────────────────────────────────────
    for doc in h.skills:
        body = strip_code(doc.body)
        lines = body.splitlines()
        has_steps = any(_ORDERED_ITEM.match(ln) for ln in lines) or any(
            _STEP_HEADING.match(ln) for ln in lines
        )
        if not has_steps:
            out.append(
                Finding(
                    "placement",
                    "placement/skill-without-steps",
                    WARN,
                    doc.rel,
                    "skill states no steps — a skill that is not a procedure is a "
                    "rule wearing a skill's clothes, and a rule that applies only "
                    "when invoked mostly does not apply",
                )
            )

    # ── subagents ─────────────────────────────────────────────────────────
    known_tools = set(runner.get("known_tools", [])) | set(cfg["extra_tools"])
    max_agent_tokens = int(cfg["max_agent_tokens"])
    chars_per_token = h.config["budget"]["chars_per_token"]
    for doc in h.agents:
        if known_tools:
            granted = [t.strip() for t in doc.frontmatter.get(tools_key, "").split(",")]
            unknown = sorted({t for t in granted if t and "__" not in t and t not in known_tools})
            for tool in unknown:
                out.append(
                    Finding(
                        "placement",
                        "placement/agent-tool-unknown",
                        ERROR,
                        doc.rel,
                        f"grants `{tool}`, which this runner has no such tool for — "
                        "an unrecognised name is accepted silently, so the agent "
                        "holds less than it says and still reports success",
                    )
                )
        cost = estimate_tokens(doc.body, chars_per_token)
        if cost > max_agent_tokens:
            out.append(
                Finding(
                    "placement",
                    "placement/agent-body-oversized",
                    WARN,
                    doc.rel,
                    f"agent body is ~{cost} tokens against a budget of "
                    f"{max_agent_tokens} — at that length it is a procedure being "
                    "run as a task, and a procedure is a skill",
                )
            )

    # ── hooks ─────────────────────────────────────────────────────────────
    hooks_root_key = runner.get("hooks_root_key", "")
    hook_events = set(runner.get("hook_events", []))
    if hooks_root_key and isinstance(h.settings, dict):
        registered = h.settings.get(hooks_root_key) or {}
        if isinstance(registered, dict):
            if hook_events:
                for event in sorted(k for k in registered if isinstance(k, str)):
                    if event not in hook_events:
                        out.append(
                            Finding(
                                "placement",
                                "placement/hook-unknown-event",
                                ERROR,
                                h.settings_rel,
                                f"`{event}` is not an event this runner fires — the "
                                "hook is registered, never runs, and reports nothing",
                            )
                        )
            if not _registered_hooks_count(h):
                out.append(
                    Finding(
                        "placement",
                        "placement/no-hooks-registered",
                        INFO,
                        h.settings_rel,
                        "no hooks are registered, so every rule in this harness is "
                        "advisory — nothing here is enforced, and prose is not a gate",
                    )
                )

    # ── honesty about coverage ────────────────────────────────────────────
    missing = []
    if not paths.get("skills"):
        missing.append("skills")
    if not paths.get("agents"):
        missing.append("subagents")
    if not hooks_root_key or not paths.get("settings"):
        missing.append("hooks")
    if missing:
        out.append(
            Finding(
                "placement",
                "placement/primitives-unavailable",
                INFO,
                ".",
                f"profile {h.profile} has no concept of {', '.join(missing)}, so "
                "guidance can only be misplaced among the primitives it does have — "
                "a short placement report here is partial measurement, not a clean bill",
            )
        )
    return out


def _registered_hooks_count(h: Harness) -> int:
    """How many hook commands the settings file actually registers.

    Counts entries rather than resolving them, because `_registered_hooks`
    deliberately drops inline one-liners — and an inline one-liner is still an
    enforced gate. The question here is whether anything is enforced at all.
    """
    runner = h.config["runner"]
    root_key = runner.get("hooks_root_key", "")
    if not h.settings or not root_key:
        return 0
    entry_key = runner.get("hooks_entry_key", "hooks")
    total = 0
    for entries in (h.settings.get(root_key) or {}).values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict):
                total += len(entry.get(entry_key) or [])
    return total


DIMENSIONS: dict[str, Callable[[Harness], list[Finding]]] = {
    "budget": check_budget,
    "redundancy": check_redundancy,
    "references": check_references,
    "completeness": check_completeness,
    "authority": check_authority,
    "executability": check_executability,
    "placement": check_placement,
}


# ── project rules ─────────────────────────────────────────────────────────
def load_project_rules(h: Harness) -> list[Finding]:
    """Run `.harnesslint/rules/*.py`, each exposing `check(harness) -> list`.

    The seven dimensions are what generalises. Every harness also has assertions
    only it can make — a board id that must not be pasted, a service that must
    always be pinned. Those belong here rather than upstream, and they are the
    reason this stays useful as a project's conventions accumulate.
    """
    rules_dir = h.root / f".{TOOL_NAME}" / "rules"
    if not rules_dir.is_dir():
        return []
    import importlib.util

    # A rule needs `from harnesslint import Finding, ERROR`. That resolves when
    # this is pip-installed, and not when it has been vendored as a loose file
    # off sys.path — which is the mode the whole design optimises for. Putting
    # our own directory on the path makes both work, so no rule file has to
    # carry an import hack.
    own_dir = str(Path(__file__).resolve().parent)
    if own_dir not in sys.path:
        sys.path.insert(0, own_dir)

    out: list[Finding] = []
    for path in sorted(rules_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(f"_hl_{path.stem}", path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
            found = module.check(h)  # type: ignore[attr-defined]
        except Exception as exc:  # a broken rule must not mask the rest
            out.append(
                Finding(
                    "project",
                    "project/rule-error",
                    ERROR,
                    path.relative_to(h.root).as_posix(),
                    f"project rule raised {type(exc).__name__}: {exc}",
                )
            )
            continue
        out.extend(found)
    return out


# ── running ───────────────────────────────────────────────────────────────
def run(h: Harness) -> list[Finding]:
    findings: list[Finding] = []
    for check in DIMENSIONS.values():
        findings.extend(check(h))
    findings.extend(load_project_rules(h))

    severities = h.config.get("severities", {})
    ignore = set(h.config.get("ignore", []))
    out = []
    for f in findings:
        if f.rule in ignore or f"{f.rule}:{f.path}" in ignore:
            continue
        if f.rule in severities:
            f = Finding(f.dimension, f.rule, severities[f.rule], f.path, f.message, f.line)
        out.append(f)
    # Total order, so two runs over the same tree emit identical bytes.
    return sorted(
        out,
        key=lambda f: (
            _SEVERITY_ORDER[f.severity],
            f.dimension,
            f.rule,
            f.path,
            f.message,
        ),
    )


# ── baseline ──────────────────────────────────────────────────────────────
def baseline_path(root: Path) -> Path:
    return root / f".{TOOL_NAME}-baseline.json"


def build_baseline(findings: list[Finding], profile: str = FALLBACK_PROFILE) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.dimension] = counts.get(f.dimension, 0) + 1
    return {
        "ruleset_version": RULESET_VERSION,
        # Recorded because a different profile measures different files. A
        # profile that flipped silently would reset the ratchet while looking
        # like an improvement.
        "profile": profile,
        "counts": dict(sorted(counts.items())),
        "fingerprints": sorted(f.fingerprint for f in findings),
    }


def compare(findings: list[Finding], baseline: dict[str, Any]) -> tuple[list[Finding], list[str]]:
    """New findings, and dimensions whose count went up.

    Regression, not perfection. An existing mess is grandfathered; adding to it
    is not. That is what lets a repo adopt this without a flag day, and it is
    the only mode in which a gate like this survives contact with a backlog.
    """
    known = set(baseline.get("fingerprints", []))
    new = [f for f in findings if f.fingerprint not in known]
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.dimension] = counts.get(f.dimension, 0) + 1
    worse = [
        f"{dim} {baseline['counts'].get(dim, 0)} -> {counts[dim]}"
        for dim in sorted(counts)
        if counts[dim] > baseline.get("counts", {}).get(dim, 0)
    ]
    return new, worse


# ── reporting ─────────────────────────────────────────────────────────────
_STATUS = {ERROR: "FAIL", WARN: "WARN", INFO: "OK"}


def render_text(h: Harness, findings: list[Finding], new: list[Finding], worse: list[str]) -> str:
    lines = [
        f"{TOOL_NAME} — {len(DIMENSIONS)} dimensions, ruleset v{RULESET_VERSION}, "
        f"profile {h.profile}",
        "",
    ]
    by_dim: dict[str, list[Finding]] = {d: [] for d in DIMENSIONS}
    for f in findings:
        by_dim.setdefault(f.dimension, []).append(f)

    inert = set(PROFILES[h.profile].inert_dimensions) if h.profile in PROFILES else set()
    for dim, items in by_dim.items():
        actionable = [f for f in items if f.severity != INFO]
        worst = min((f.severity for f in items), key=lambda s: _SEVERITY_ORDER[s], default=INFO)
        if dim in inert and not items:
            # "clean" would be a lie here: nothing was measured. Saying so is the
            # same discipline the tool applies to the harnesses it checks.
            lines.append(f"  {dim:<14} {'not measured (profile)':<28} —")
            continue
        note = f"{len(actionable)} finding(s)" if actionable else "clean"
        lines.append(f"  {dim:<14} {note:<28} {_STATUS[worst]}")

    lines.append("")
    if findings:
        lines.append("Findings")
        for f in findings:
            marker = "NEW " if f in new else "    "
            lines.append(f"  {marker}{f.severity:<5} {f.rule}")
            lines.append(f"        {f.path}: {f.message}")
        lines.append("")
    if worse:
        lines.append("Regression vs baseline: " + "; ".join(worse))
    elif new:
        lines.append(f"{len(new)} new finding(s) not in baseline")
    else:
        lines.append("No regression vs baseline")
    return "\n".join(lines)


def render_github(findings: list[Finding]) -> str:
    """GitHub Actions annotations — clickable in the PR diff."""
    out = []
    for f in findings:
        if f.severity == INFO:
            continue
        level = "error" if f.severity == ERROR else "warning"
        msg = f.message.replace("\n", " ")
        out.append(f"::{level} file={f.path},line={max(1, f.line)}::[{f.rule}] {msg}")
    return "\n".join(out)


# ── cli ───────────────────────────────────────────────────────────────────
def die(msg: str) -> None:
    print(f"{TOOL_NAME}: {msg}", file=sys.stderr)
    raise SystemExit(2)


def _repo_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            return candidate
    return here


_SAMPLE_CONFIG = """\
# harnesslint — this project's budgets and opinions.
#
# Every key overrides a default in harnesslint.py. Deleting this file is valid:
# the tool runs on its built-in defaults. Keep here only what YOU decided
# differently, and say why — the reason is the part that ages well.

# [runner]
# Which agent runner this repo uses. Omitted, it is detected from the tree; run
# `harnesslint profiles` to see the options and which one you are getting. Pin
# it once the answer matters — a profile that flips changes what gets measured.
# profile = "claude-code"    # or "agents-md", "generic"

# [paths]
# The profile already sets these. Override only what your repo does
# differently — for example a second location for commands.
# commands = [".claude/commands/**/*.md", "tools/commands/**/*.md"]

# [budget]
# What the harness costs on EVERY turn: root memory plus every artifact's
# description. Bodies are NOT counted — they load on invocation, so a long
# careful procedure is cheap and a long description is not.
# always_loaded_tokens = 24000

[executability]
# CLIs your harness may assume without saying so.
# assumed_clis = ["git", "python3", "bash", "grep", "sed", "jq"]

# Depended on, and said OUT LOUD. The point is not to forbid a dependency but
# to make an environment that lacks it a stated precondition, rather than a
# surprise found halfway through a task.
declared_clis = []

# [placement]
# Is each piece of guidance in the primitive whose job it is? See
# .harnesslint/PRIMITIVES.md for the taxonomy these numbers are measuring.
#
# Consecutive numbered steps allowed in the always-loaded file before it is
# calling itself a procedure. A procedure belongs in a skill.
# max_memory_steps = 6
#
# Tool names your runner has that the profile cannot know about — plugin tools,
# mostly. Without this a real grant reads as a typo, which is worse than silence.
# extra_tools = []

# [severities]
# "redundancy/duplicate-rule" = "info"

# [ignore]
# Prefer the baseline. A baselined finding still prints in `report`; an ignored
# one never does, and you almost always want to know if a category grows.
"""

_SAMPLE_RULE = '''\
"""An example project rule. Delete it, or make it yours.

The seven built-in dimensions are what generalises across harnesses. Assertions
only THIS project can make belong here — a magic id that must never be pasted,
a service flag that must always be present, a convention your team agreed on
and keeps half-forgetting.

Contract: expose `check(harness) -> list[Finding]`. The harness object carries
`.root`, `.config`, `.memory`, `.commands`, `.agents`, `.skills`, `.settings`
and `.all_docs`. A rule that raises is reported as `project/rule-error` and
does not mask the others.
"""

from harnesslint import WARN, Finding, Harness


def check(h: Harness) -> list[Finding]:
    out: list[Finding] = []
    for doc in h.all_docs:
        for n, line in enumerate(doc.raw.splitlines(), 1):
            if "TODO" in line:
                out.append(
                    Finding(
                        "project",
                        "project/todo-in-harness",
                        WARN,
                        doc.rel,
                        "a TODO in harness config is a rule nobody follows yet",
                        n,
                    )
                )
    return out
'''

_PRIMITIVES_DOC = """\
# Five primitives, five jobs

An agent harness is not one thing, it is five, and each has a job. Guidance put
in the wrong one still reads correctly to a person and quietly does the wrong
thing to the agent — which is the shape every dimension in this tool is aimed
at. The `placement` dimension checks this mechanically; the taxonomy below is
the part worth remembering.

**Project instructions file** — the handful of facts true for EVERY task. Build
command, test command, the house conventions nothing enforces, the one
architectural rule everyone gets wrong. Short, because it is paid for on every
turn and a long one is read closely on none.

**Scoped rules** — conventions that apply to part of the tree, loaded on
contact. This is how the always-loaded file stays small without losing the
guidance, and it is the main lever you have on always-loaded cost.

**Skills** — a procedure with steps, invoked when a kind of task comes up.
Invocation is what a skill costs, so length is cheap here and expensive in the
instructions file. That is the whole reason procedures belong here.

**Subagents** — a bounded task whose exploration is large and whose answer is
small. The isolation is the point; if the body has grown into a procedure, it
wanted to be a skill.

**Hooks** — anything that MUST happen, or must not. Prose is advice. A hook is
the only primitive here that is a gate, and a harness with none enforces
nothing, however firmly it is worded.

## Which rule speaks to each

Instructions file

- `placement/no-instruction-file` — commands, agents or hooks exist with no
  root instruction file to steer from.
- `placement/procedure-in-memory` — a long run of numbered steps in the
  always-loaded file. That is a skill.

Scoped rules

- `placement/scope-matches-nothing` — a scoped file over a directory holding
  nothing else. The rule governs no file and does not say so.
- `placement/scope-not-declared` — a runner where scope is declared in
  frontmatter, and this file declares none.

Skills

- `placement/skill-without-steps` — a skill with no steps is a rule wearing a
  skill's clothes, and a rule that applies only when invoked mostly does not.

Subagents

- `placement/agent-tool-unknown` — a name the runner has no such tool for. It
  is accepted silently, so the agent holds less than it says.
- `placement/agent-body-oversized` — an agent body long enough to be a
  procedure. A procedure is a skill.

Hooks

- `placement/hook-unknown-event` — a hook on an event the runner never fires.
  It never runs and never reports that it did not.
- `placement/no-hooks-registered` — nothing is enforced anywhere. A fact rather
  than a failure, which is why it is INFO.

Coverage

- `placement/primitives-unavailable` — the active profile has no concept of
  some of these, so a short report here is partial measurement, not a clean
  bill of health.

Three rules are deliberately absent, and the omissions are the design. An
obligation stated in prose that no hook enforces, a subagent with no return
contract, and a rule in the root file that ought to be scoped all require
reading intent out of prose. A checker that guesses is one people learn to
ignore, so those stay unshipped until there is a mechanical signal for them.

Run `harnesslint explain placement` for the same taxonomy from the tool.
"""


_STUB_MEMORY = """\
# Project instructions

Everything here is loaded on EVERY turn, so it holds only what is true for every
task. A procedure with steps belongs in a skill; a convention that applies to
one directory belongs in an instruction file inside that directory.

## Commands

- Build: `<fill this in>`
- Test: `<fill this in>`

## Conventions

- The one architectural rule people get wrong here: `<fill this in>`
"""

_STUB_SKILL = """\
---
name: example
description: Replace this with when to invoke the skill, not what it contains.
---

# Example skill

A skill is a procedure. Its body is paid for on invocation rather than on every
turn, which is what makes it the right home for steps.

1. State the precondition this procedure assumes.
2. Do the first thing.
3. Do the second thing.
4. Say what done looks like, so the agent can tell when it is.
"""

_STUB_SCOPED = """\
# Instructions for this directory

Loaded when the agent touches a file here, and not otherwise. This is how the
always-loaded file stays short without losing the guidance.

- A convention that is true here and nowhere else: `<fill this in>`
"""

_STUB_SETTINGS = {
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": "echo 'replace this with the gate you actually want'",
                    }
                ],
            }
        ]
    }
}

_CI_SNIPPET = """
Add this job to your CI. `--format github` emits ::error / ::warning
annotations, so a finding is clickable on the PR diff rather than buried in
log output:

  harness:
    name: harness gate
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with: { python-version: "3.11" }
      - run: python3 harnesslint.py check --format github

Then freeze today as the floor, so CI fails on regression rather than on the
mess you already have:

  python3 harnesslint.py report     # read what you are about to accept
  python3 harnesslint.py baseline   # commit the result
"""


def _scaffold_dir(root: Path, exclude: Iterable[str]) -> Path | None:
    """A top-level directory a scoped rule would actually govern, or None.

    The scaffold refuses to write a scoped rule into an empty tree, because a
    rule that governs no file is exactly what `placement/scope-matches-nothing`
    exists to report — a scaffold that fails its own dimension teaches the wrong
    thing on the first run. Picking the first candidate in sorted order keeps a
    second `init` on the same tree writing to the same place.
    """
    skip = {f".{TOOL_NAME}", "node_modules", "vendor", "__pycache__"}
    for entry in sorted(root.iterdir(), key=lambda e: e.name):
        if not entry.is_dir() or entry.name.startswith(".") or entry.name in skip:
            continue
        if any(fnmatch.fnmatch(entry.name, ex) for ex in exclude):
            continue
        if any(child.is_file() for child in entry.rglob("*")):
            return entry
    return None


def init(root: Path, scaffold: bool = False) -> int:
    """Scaffold a config and a rules directory, then print the CI snippet.

    Everything it writes is optional — the tool runs on defaults with no config
    at all. This exists because the first question in a new repo is always
    "where do I put things", and answering that in a file beats answering it in
    a README nobody opens. It never overwrites: re-running is safe.

    `--scaffold` additionally stubs out the primitives themselves. It is opt-in
    because writing into someone's runner directory uninvited is not a linter's
    business — everything without the flag stays under `.{TOOL_NAME}/`, which
    this tool owns.
    """
    config = root / f"{TOOL_NAME}.toml"
    tool_dir = root / f".{TOOL_NAME}"
    rules_dir = tool_dir / "rules"
    made: list[str] = []

    def write(path: Path, text: str) -> None:
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        made.append(path.relative_to(root).as_posix())

    write(config, _SAMPLE_CONFIG)
    write(rules_dir / "example.py", _SAMPLE_RULE)
    # Always: the taxonomy the placement dimension measures against. Under
    # .harnesslint/, so it costs nothing on a turn and touches nothing the
    # runner owns.
    write(tool_dir / "PRIMITIVES.md", _PRIMITIVES_DOC)

    note = ""
    if scaffold:
        # Detection runs against the tree as it is now, which for an empty one
        # answers "generic" — there is no runner to detect yet. Scaffolding is
        # the act of choosing a layout, so an undetected tree gets the runner
        # this profile set knows best rather than nothing at all. A tree that
        # HAS a runner keeps it, so `agents-md` gets instruction files only.
        profile = detect_profile(root)
        if profile.name == FALLBACK_PROFILE:
            profile = PROFILES["claude-code"]
        write(root / "CLAUDE.md", _STUB_MEMORY)
        if profile.paths.get("skills"):
            write(root / ".claude" / "skills" / "example" / "SKILL.md", _STUB_SKILL)
        if profile.paths.get("settings"):
            write(root / ".claude" / "settings.json", json.dumps(_STUB_SETTINGS, indent=2) + "\n")
        target = _scaffold_dir(root, profile.paths.get("exclude", []))
        if target is not None:
            write(target / "CLAUDE.md", _STUB_SCOPED)
        else:
            note = (
                f"{TOOL_NAME}: no directory with files in it, so no scoped rule was "
                "written — a scoped rule over an empty directory is a dead rule, "
                "which is what placement/scope-matches-nothing reports"
            )

    print(f"{TOOL_NAME}: wrote {', '.join(made)}" if made else f"{TOOL_NAME}: already set up")
    if note:
        print(note)
    print(_CI_SNIPPET)
    return 0


def list_profiles(root: Path) -> int:
    """Which runners are known, which one this tree gets, and what each misses.

    The last column is the point. A profile with inert dimensions is not doing
    seven things badly, it is doing four things and saying so — and a user who
    cannot see that would read a short report as a clean harness.
    """
    detected = detect_profile(root)
    print(f"{TOOL_NAME} {__version__} — known runner profiles\n")
    for profile in PROFILES.values():
        mark = "*" if profile.name == detected.name else " "
        print(f" {mark} {profile.name}")
        print(f"     {profile.summary}")
        markers = ", ".join(profile.detect) if profile.detect else "(fallback — matches anything)"
        print(f"     detected by: {markers}")
        inert = profile.inert_dimensions
        print(f"     not measured: {', '.join(inert) if inert else 'nothing — all seven apply'}")
        print()
    print(f"* = detected for this tree ({root})")
    print(f'Pin it with `[runner] profile = "..."` in {TOOL_NAME}.toml.')
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=TOOL_NAME, description=__doc__.split("\n\n")[1])
    parser.add_argument(
        "command", choices=["check", "baseline", "report", "explain", "init", "profiles"]
    )
    parser.add_argument("target", nargs="?", help="rule id, for `explain`")
    parser.add_argument("--root", help="repo root (default: nearest .git ancestor)")
    parser.add_argument(
        "--version",
        action="version",
        version=f"{TOOL_NAME} {__version__} (ruleset v{RULESET_VERSION})",
    )
    parser.add_argument("--format", choices=["text", "json", "github"], default="text")
    parser.add_argument(
        "--scaffold",
        action="store_true",
        help="`init` only: also stub out the primitives themselves",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="ignore the baseline; any error-severity finding fails",
    )
    args = parser.parse_args(argv)

    root = _repo_root(args.root)

    if args.command == "init":
        return init(root, scaffold=args.scaffold)
    if args.command == "profiles":
        return list_profiles(root)

    config = load_config(root)
    h = discover(root, config)

    if args.command == "explain":
        if not args.target:
            die("explain needs a rule id, e.g. `explain budget/always-loaded`")
        fn = DIMENSIONS.get(args.target.split("/")[0])
        if fn is None or not fn.__doc__:
            die(f"unknown rule or dimension: {args.target}")
        print(f"{args.target}\n\n{fn.__doc__.strip()}")
        return 0

    findings = run(h)

    if args.command == "baseline":
        data = build_baseline(findings, h.profile)
        baseline_path(root).write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(
            f"{TOOL_NAME}: baseline written with {len(findings)} finding(s) "
            f"across {len(data['counts'])} dimension(s), profile {h.profile}"
        )
        return 0

    bpath = baseline_path(root)
    baseline = json.loads(bpath.read_text(encoding="utf-8")) if bpath.is_file() else None
    if baseline and baseline.get("ruleset_version") != RULESET_VERSION:
        print(
            f"{TOOL_NAME}: baseline was recorded under ruleset "
            f"v{baseline.get('ruleset_version')}, this is v{RULESET_VERSION} — "
            f"re-run `{TOOL_NAME} baseline` after reviewing the diff",
            file=sys.stderr,
        )
        baseline = None
    if baseline and baseline.get("profile", h.profile) != h.profile:
        # A different profile reads different files, so its counts are not
        # comparable. Trusting them would grandfather findings nobody accepted.
        print(
            f"{TOOL_NAME}: baseline was recorded under profile "
            f"{baseline.get('profile')!r}, this run detected {h.profile!r} — "
            f"re-run `{TOOL_NAME} baseline` after reviewing the diff, or pin the profile "
            f"with `[runner] profile` in {TOOL_NAME}.toml",
            file=sys.stderr,
        )
        baseline = None

    new, worse = compare(findings, baseline) if baseline else (findings, [])

    if args.format == "json":
        print(
            json.dumps(
                {
                    "tool_version": __version__,
                    "ruleset_version": RULESET_VERSION,
                    "profile": h.profile,
                    "findings": [asdict(f) | {"fingerprint": f.fingerprint} for f in findings],
                    "new": [f.fingerprint for f in new],
                    "regressions": worse,
                },
                indent=2,
                sort_keys=True,
            )
        )
    elif args.format == "github":
        print(render_github(findings))
    else:
        print(render_text(h, findings, new, worse))

    if args.command == "report":
        return 0
    if args.strict:
        return 1 if any(f.severity == ERROR for f in findings) else 0
    if worse or any(f.severity == ERROR for f in new):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
