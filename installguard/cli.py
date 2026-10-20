"""Command-line entry point for installguard.

Subcommands:

``check``   Vet packages named on the command line.
``hook``    Read a Claude Code ``PreToolUse`` payload on stdin and decide.
``init``    Write the hook configuration into ``~/.claude/settings.json``.
``parse``   Show what the command parser extracts from a shell command.
            A debugging aid — it performs no network calls.

Only ``parse`` is implemented so far; the others report that plainly and exit
non-zero rather than pretending to vet anything. A security tool that appears
to run but checks nothing is worse than one that admits it is unfinished.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import __version__
from .parse import Ecosystem, parse_command

#: Exit codes. 0 success, 1 red flags found, 2 usage error (argparse's own),
#: 69 unavailable — borrowed from sysexits.h EX_UNAVAILABLE.
EXIT_OK = 0
EXIT_FLAGGED = 1
EXIT_UNIMPLEMENTED = 69


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="installguard",
        description="Vet npm and PyPI packages for supply-chain red flags "
        "before they install.",
    )
    parser.add_argument(
        "--version", action="version", version=f"installguard {__version__}"
    )

    subcommands = parser.add_subparsers(dest="command", metavar="<command>")

    check = subcommands.add_parser(
        "check", help="vet one or more named packages"
    )
    check.add_argument("packages", nargs="+", metavar="PACKAGE")
    check.add_argument(
        "-e",
        "--ecosystem",
        choices=[item.value for item in Ecosystem],
        default=Ecosystem.NPM.value,
        help="registry to query (default: npm)",
    )

    subcommands.add_parser(
        "hook", help="run as a Claude Code PreToolUse hook (reads stdin)"
    )
    subcommands.add_parser(
        "init", help="configure the hook in ~/.claude/settings.json"
    )

    show = subcommands.add_parser(
        "parse", help="show what the parser extracts from a shell command"
    )
    show.add_argument("shell_command", metavar="COMMAND")

    return parser


def _cmd_parse(shell_command: str) -> int:
    """Print the parsed installs for ``shell_command``. No network access."""
    installs = parse_command(shell_command)
    if not installs:
        print("No package installs detected.")
        return EXIT_OK

    for install in installs:
        print(f"{install.manager} ({install.ecosystem})")
        for spec in install.specs:
            print(f"  package  {spec.display}")
        if install.installs_manifest:
            print("  manifest install (package.json / requirements.txt)")
        for target in install.skipped:
            print(f"  skipped  {target}  (no registry metadata)")
    return EXIT_OK


def _unimplemented(name: str) -> int:
    print(
        f"installguard {name}: not implemented yet.\n"
        f"Registry lookups are still in development — see "
        f"https://github.com/gauravmassand/installguard",
        file=sys.stderr,
    )
    return EXIT_UNIMPLEMENTED


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return EXIT_OK
    if args.command == "parse":
        return _cmd_parse(args.shell_command)
    return _unimplemented(args.command)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
