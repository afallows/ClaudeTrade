"""Guards against documentation drifting out of sync with the real CLI.

The failure mode this catches: a doc page names a `claudetrade <command>` (or
`claudetrade <group> <subcommand>`) that doesn't exist, or stops naming one
that does -- exactly the kind of staleness this documentation pass was asked
to fix (see README.md, docs/windows-install.md, docs/windows-smoke-test.md).

This intentionally does not try to check *option* spelling (`--export` vs
`--out`, etc.) or prose accuracy -- that would be far more brittle for far
less benefit. It only checks that every `claudetrade <word> [<word>]` shown as
a command in a fenced code block resolves to something the real Typer app
registers, or is on the small, explicit allowlist of commands the docs
deliberately mention as *not* existing (documenting a gap by name, e.g.
"there is no `claudetrade export` command").
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from claudetrade.cli import app, backtest_app, db_app, paper_app, secrets_app, verify_app

REPO_ROOT = Path(__file__).resolve().parents[1]

DOC_FILES = [
    REPO_ROOT / "README.md",
    *sorted((REPO_ROOT / "docs").glob("*.md")),
]

#: Command names deliberately mentioned in the docs as gaps -- things that do
#: NOT exist and are documented as such (known-limitations.md, roadmap.md).
#: A doc is allowed to say "there is no `claudetrade export` command" without
#: that being flagged as a stale/broken command reference.
DOCUMENTED_AS_NOT_IMPLEMENTED = {
    ("export", None),
    ("validate-config", None),
    ("paper", "submit"),
}

#: Prose-only headings/mentions that look like `claudetrade <word>` but are
#: not command invocations (e.g. a markdown heading naming the whole CLI).
#: Matched as an exact (group, sub) pair after normalisation.
NOT_A_COMMAND = {
    ("help", None),  # `claudetrade --help` output banner text, not a command
}


def _typer_commands(typer_app) -> set[str]:
    return {c.name or c.callback.__name__.replace("_", "-") for c in typer_app.registered_commands}


def _typer_group_names(typer_app) -> set[str]:
    """Names of sub-Typer apps mounted on ``typer_app`` (e.g. `app.add_typer(secrets_app, ...)`)."""
    return {g.name for g in typer_app.registered_groups}


def _real_command_surface() -> set[tuple[str, str | None]]:
    """(group_or_top_level_command, subcommand_or_None) pairs the real CLI supports."""
    surface: set[tuple[str, str | None]] = {(name, None) for name in _typer_commands(app)}

    group_apps = {
        "secrets": secrets_app,
        "paper": paper_app,
        "db": db_app,
        "verify": verify_app,
        "backtest": backtest_app,
    }
    # Every group actually mounted on `app` (via add_typer) must be one of the
    # ones this test knows about -- catches a new group being added to cli.py
    # without this test being updated to introspect it too.
    assert _typer_group_names(app) <= set(group_apps), (
        f"cli.py registers group(s) {_typer_group_names(app) - set(group_apps)} that this "
        "test doesn't know how to introspect -- update _real_command_surface()"
    )
    for name, sub_app in group_apps.items():
        for sub in _typer_commands(sub_app):
            surface.add((name, sub))
    return surface


#: Matches `claudetrade word1 [word2]` where word1/word2 look like command
#: names (lowercase, digits, hyphen, underscore) -- deliberately does not
#: match option flags (`--foo`), paths, or symbols. The negative lookbehind
#: excludes `--cov=claudetrade tests/`-style pytest invocations, where
#: "claudetrade" is a package-name argument value, not the CLI being invoked.
_COMMAND_PATTERN = re.compile(
    r"(?<![=\w.])claudetrade\s+([a-z][a-z0-9_-]*)(?:\s+([a-z][a-z0-9_-]*))?"
)

#: Flags/options and other non-command words that can legally follow
#: `claudetrade` or a group name without being a second command word.
_NOT_A_WORD = {
    "version",  # also a real top-level command, handled via the real surface
}


def _iter_fenced_code_blocks(text: str) -> list[str]:
    """Contents of every ``` fenced code block in a markdown file."""
    return re.findall(r"```(?:[a-zA-Z0-9_+-]*)\n(.*?)```", text, flags=re.DOTALL)


def _command_mentions_in_code_blocks(path: Path) -> list[tuple[str, str | None, int]]:
    """Every ``claudetrade <cmd> [<sub>]`` invocation found in fenced code, with line numbers."""
    text = path.read_text(encoding="utf-8")
    mentions: list[tuple[str, str | None, int]] = []
    for block in _iter_fenced_code_blocks(text):
        block_start_line = text[: text.index(block)].count("\n") + 1
        for match in _COMMAND_PATTERN.finditer(block):
            line_no = block_start_line + block[: match.start()].count("\n")
            top, sub = match.group(1), match.group(2)
            mentions.append((top, sub, line_no))
    return mentions


REAL_SURFACE = _real_command_surface()
TOP_LEVEL_GROUPS = {"secrets", "paper", "db", "verify", "backtest"}


@pytest.mark.parametrize("doc_path", DOC_FILES, ids=lambda p: p.name)
def test_documented_commands_exist_in_cli(doc_path: Path) -> None:
    """Every `claudetrade <cmd> [<sub>]` shown in a code block is real, or explicitly
    flagged in the doc as not existing."""
    problems = []
    for top, sub, line_no in _command_mentions_in_code_blocks(doc_path):
        key = (top, sub) if top in TOP_LEVEL_GROUPS else (top, None)

        if key in REAL_SURFACE:
            continue
        if key in DOCUMENTED_AS_NOT_IMPLEMENTED:
            continue
        if key in NOT_A_COMMAND:
            continue
        # A bare group name with no subcommand shown yet (e.g. `claudetrade
        # secrets --help`) is fine -- only flag it once a specific, wrong
        # subcommand is named.
        if top in TOP_LEVEL_GROUPS and sub is None:
            continue

        problems.append(f"{doc_path.relative_to(REPO_ROOT)}:{line_no}: `claudetrade {top}"
                         f"{' ' + sub if sub else ''}` is not a real command and is not on "
                         f"the DOCUMENTED_AS_NOT_IMPLEMENTED allowlist in this test")

    assert not problems, "\n".join(problems)


def test_real_cli_surface_is_nonempty() -> None:
    """Sanity check that introspection itself is working (catches a silent no-op above)."""
    assert ("init", None) in REAL_SURFACE
    assert ("scan", None) in REAL_SURFACE
    assert ("secrets", "set") in REAL_SURFACE
    assert ("paper", "kill-switch") in REAL_SURFACE
    assert ("db", "backup") in REAL_SURFACE
    assert ("verify", "ledger") in REAL_SURFACE
    assert ("backtest", "report") in REAL_SURFACE


def test_documented_app_dir_matches_config_default() -> None:
    """The Windows/macOS/Linux default data-directory claims in the docs must match
    ``default_app_dir()`` in ``src/claudetrade/config.py`` (read-only; not modified here)."""
    import inspect

    from claudetrade.config import default_app_dir

    source = inspect.getsource(default_app_dir)
    assert "LOCALAPPDATA" in source, (
        "docs/windows-install.md and others document %LOCALAPPDATA%\\ClaudeTrade as the "
        "Windows default app directory -- default_app_dir() no longer checks LOCALAPPDATA, "
        "so those docs are now stale."
    )
