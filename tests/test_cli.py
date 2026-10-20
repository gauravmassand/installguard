"""Tests for the command-line interface."""

from __future__ import annotations

import pytest

from pkgpatrol import __version__
from pkgpatrol.cli import EXIT_OK, EXIT_UNIMPLEMENTED, main


def test_version_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    # argparse's `version` action raises SystemExit(0) after printing.
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_args_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == EXIT_OK
    assert "usage: pkgpatrol" in capsys.readouterr().out


def test_parse_subcommand_lists_packages(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["parse", "npm i express@4.18.0"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "express@4.18.0" in out


def test_parse_subcommand_reports_nothing_found(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["parse", "ls -la"]) == EXIT_OK
    assert "No package installs detected" in capsys.readouterr().out


def test_parse_subcommand_reports_skipped(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["parse", "npm i git+https://github.com/o/r.git"]) == EXIT_OK
    assert "skipped" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["check", "hook", "init"])
def test_unimplemented_commands_fail_loudly(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """These must not exit 0 — a security tool that silently checks nothing
    is worse than one that admits it is unfinished."""
    argv = [command, "express"] if command == "check" else [command]
    assert main(argv) == EXIT_UNIMPLEMENTED
    assert "not implemented" in capsys.readouterr().err


def test_bad_subcommand_is_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["nonsense"])
    assert excinfo.value.code == 2
