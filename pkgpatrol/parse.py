"""Extract package-install intent from shell command strings.

Claude Code hands us a raw ``Bash`` command, which may be a pipeline, an
``&&`` chain, or a one-liner with redirections. This module finds the package
installs inside it and normalizes them into :class:`PackageSpec` objects.

Design stance: **over-detect rather than under-detect.** A false positive costs
one wasted registry lookup; a false negative means an unvetted package lands on
disk. Where a token is ambiguous we treat it as a package and let the registry
lookup decide.

Local and VCS targets (``./pkg``, ``git+https://…``, ``foo.tgz``,
``-r requirements.txt``) are deliberately *not* returned — there is no registry
metadata to check, so they fall outside what pkgpatrol can vet.

Layout
------
1. Public types        — what callers receive
2. Ecosystem rules     — how each registry names and versions packages
3. Manager registry    — one record per package manager; the only place to
                         edit when adding support for a new one
4. Shell tokenizing    — splitting a command into comparable argument lists
5. Identification      — deciding which manager (if any) a segment invokes
6. Target extraction   — turning arguments into validated specs
7. Entry point         — :func:`parse_command`
"""

from __future__ import annotations

import posixpath
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "Ecosystem",
    "PackageSpec",
    "InstallCommand",
    "parse_command",
    "split_segments",
]


# ---------------------------------------------------------------------------
# 1. Public types
# ---------------------------------------------------------------------------


class Ecosystem(str, Enum):
    """A package registry we know how to query."""

    NPM = "npm"
    PYPI = "pypi"

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


@dataclass(frozen=True)
class PackageSpec:
    """One package requested by an install command."""

    name: str
    ecosystem: Ecosystem
    version_spec: str | None = None
    raw: str = ""

    @property
    def display(self) -> str:
        return f"{self.name}@{self.version_spec}" if self.version_spec else self.name


@dataclass
class InstallCommand:
    """A single install invocation found inside a shell command."""

    manager: str
    ecosystem: Ecosystem
    specs: list[PackageSpec] = field(default_factory=list)
    raw: str = ""
    #: True when the command installs from a manifest/lockfile rather than from
    #: named packages (``npm ci``, bare ``yarn``, ``pip install -r req.txt``).
    installs_manifest: bool = False
    #: Targets recognized as installs but not vettable (paths, git URLs, …).
    skipped: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 2. Ecosystem rules
# ---------------------------------------------------------------------------


def _split_npm_target(token: str) -> tuple[str, str | None]:
    """Split an npm target into ``(name, version_spec)``.

    The leading ``@`` of a scoped name is part of the name, so the version
    separator is the *second* ``@``: ``@scope/pkg@^1.2.3`` →
    ``("@scope/pkg", "^1.2.3")``.
    """
    if token.startswith("@"):
        separator = token.find("@", 1)
        if separator == -1:
            return token, None
        return token[:separator], token[separator + 1 :] or None

    name, found, version = token.partition("@")
    return name, (version or None) if found else None


_PYPI_VERSION_DELIM = re.compile(r"(===|==|>=|<=|~=|!=|>|<|@)")


def _split_pypi_target(token: str) -> tuple[str, str | None]:
    """Split a PEP 508 requirement into ``(name, version_spec)``.

    Extras are dropped from the name (``requests[socks]>=2`` → ``requests``),
    since the registry is queried by project name alone.
    """
    # Environment markers: `requests; python_version < "3.11"`.
    token = token.split(";", 1)[0].strip()

    bracket = token.find("[")
    if bracket != -1:
        closing = token.find("]", bracket)
        name = token[:bracket]
        remainder = token[closing + 1 :] if closing != -1 else ""
    else:
        delimiter = _PYPI_VERSION_DELIM.search(token)
        if not delimiter:
            return token.strip(), None
        name = token[: delimiter.start()]
        remainder = token[delimiter.start() :]

    return name.strip(), remainder.strip() or None


@dataclass(frozen=True)
class EcosystemRules:
    """How one registry names packages, and which flags take values.

    ``value_flags`` is per-ecosystem on purpose. A single shared table is
    unsafe: ``-f`` is pip's ``--find-links`` (takes a value) but npm's
    ``--force`` (boolean), so a shared table consumes the package name in
    ``npm i -f lodash`` and the install sails through unvetted. Same for
    ``-t``. Getting this wrong fails *open* — the one outcome this tool
    cannot afford.
    """

    name_pattern: re.Pattern[str]
    split_target: Callable[[str], tuple[str, str | None]]
    value_flags: frozenset[str]


# Grouped a few per line rather than one-per-line: these are reference tables
# copied from each tool's CLI docs, and scanning them for a missing flag is the
# maintenance task that matters. `fmt: off` fences the whole statement, which
# is the only level at which ruff format honors it.
# fmt: off
_NPM_VALUE_FLAGS = frozenset({
    "--registry", "--prefix", "-C", "--cache", "--userconfig",
    "--globalconfig", "--workspace", "-w", "--omit", "--include",
    "--save-prefix", "--tag", "--access", "--otp", "--loglevel",
    "--before", "--cpu", "--os", "--node-options", "--script-shell",
    "--store-dir", "--virtual-store-dir", "--filter", "--config-file",
    "--cwd", "--dir", "--reporter", "--mode", "--shell", "--call", "-c",
    "--node-linker", "--resolution-mode",
})

_PYPI_VALUE_FLAGS = frozenset({
    "-r", "--requirement", "-c", "--constraint", "-e", "--editable",
    "-i", "--index-url", "--extra-index-url", "--find-links", "-f",
    "--target", "-t", "--platform", "--python-version", "--implementation",
    "--abi", "--root", "--prefix", "--src", "--upgrade-strategy",
    "--no-binary", "--only-binary", "--progress-bar", "--cert",
    "--client-cert", "--proxy", "--retries", "--timeout", "--exists-action",
    "--trusted-host", "--log", "--cache-dir", "--report",
    "--config-settings", "-C", "--python", "-p", "--index",
    "--default-index", "--source", "--group", "-G", "--extra", "--with",
    "--spec", "--pip-args", "--index-strategy", "--keyring-provider",
})
# fmt: on

#: npm package names: optional ``@scope/``, no leading dot or underscore.
#: Case-insensitive — npm rejects uppercase for new packages, but legacy ones
#: exist, and a 404 from the registry beats silently skipping the lookup.
_NPM_NAME = re.compile(
    r"^(?:@[a-z0-9][a-z0-9\-._~]*/)?[a-z0-9][a-z0-9\-._~]*$", re.IGNORECASE
)

#: PEP 508 project names.
_PYPI_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._\-]*[A-Za-z0-9])?$", re.ASCII)

ECOSYSTEMS: dict[Ecosystem, EcosystemRules] = {
    Ecosystem.NPM: EcosystemRules(
        name_pattern=_NPM_NAME,
        split_target=_split_npm_target,
        value_flags=_NPM_VALUE_FLAGS,
    ),
    Ecosystem.PYPI: EcosystemRules(
        name_pattern=_PYPI_NAME,
        split_target=_split_pypi_target,
        value_flags=_PYPI_VALUE_FLAGS,
    ),
}

#: Flags that mean "install from a requirements/constraints file".
_MANIFEST_FLAGS = frozenset({"-r", "--requirement"})


# ---------------------------------------------------------------------------
# 3. Manager registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Manager:
    """Everything we know about one package manager.

    To support a new manager, add one entry to :data:`MANAGERS`. Nothing else
    in this module should need editing.
    """

    #: Binary name as invoked (``npm``, ``pip3``, ``poetry``).
    binary: str
    ecosystem: Ecosystem
    #: Subcommands that install named packages (``npm install``, ``yarn add``).
    install_subcommands: frozenset[str] = frozenset()
    #: Subcommands that install from a manifest with no named packages
    #: (``npm ci``, ``poetry install``, ``uv sync``).
    manifest_subcommands: frozenset[str] = frozenset()
    #: Flags whose value is itself a package to vet (``npx -p typescript``).
    #: These one-shot runners never write to a manifest, but the package is
    #: still downloaded and executed — which is the actual threat.
    package_flags: frozenset[str] = frozenset()
    #: True when the bare binary installs the manifest (``yarn``).
    bare_is_manifest: bool = False
    #: True for one-shot runners (``npx pkg``): arguments are packages
    #: directly, with no install subcommand to match.
    is_runner: bool = False
    #: True when a package-less invocation installs the manifest. Set for
    #: project managers (``npm install``), not for runners.
    empty_is_manifest: bool = False


# npm ships a long list of `install` aliases, including typo-tolerant ones.
# `it` / `install-test` / `cit` / `install-ci-test` install *and* run tests.
# fmt: off
_NPM_INSTALL_ALIASES = frozenset({
    "install", "i", "add", "in", "ins", "inst", "insta", "instal",
    "isnt", "isnta", "isntal", "isntall",
    "install-test", "it", "install-ci-test", "cit",
})
# fmt: on

_DLX_PACKAGE_FLAGS = frozenset({"-p", "--package"})

MANAGERS: dict[str, Manager] = {
    "npm": Manager(
        "npm",
        Ecosystem.NPM,
        install_subcommands=_NPM_INSTALL_ALIASES,
        manifest_subcommands=frozenset({"ci", "install", "i"}),
        empty_is_manifest=True,
    ),
    "yarn": Manager(
        "yarn",
        Ecosystem.NPM,
        install_subcommands=frozenset({"add", "install"}),
        manifest_subcommands=frozenset({"install"}),
        bare_is_manifest=True,
        empty_is_manifest=True,
    ),
    "pnpm": Manager(
        "pnpm",
        Ecosystem.NPM,
        install_subcommands=frozenset({"add", "install", "i"}),
        manifest_subcommands=frozenset({"install", "i"}),
        empty_is_manifest=True,
    ),
    "bun": Manager(
        "bun",
        Ecosystem.NPM,
        install_subcommands=frozenset({"add", "install"}),
        manifest_subcommands=frozenset({"install"}),
        empty_is_manifest=True,
    ),
    "pip": Manager("pip", Ecosystem.PYPI, install_subcommands=frozenset({"install"})),
    "pip3": Manager("pip3", Ecosystem.PYPI, install_subcommands=frozenset({"install"})),
    "pipx": Manager(
        "pipx",
        Ecosystem.PYPI,
        install_subcommands=frozenset({"install", "inject", "run"}),
    ),
    "uv": Manager(
        "uv",
        Ecosystem.PYPI,
        install_subcommands=frozenset({"add"}),
        manifest_subcommands=frozenset({"sync"}),
        empty_is_manifest=True,
    ),
    "poetry": Manager(
        "poetry",
        Ecosystem.PYPI,
        install_subcommands=frozenset({"add"}),
        manifest_subcommands=frozenset({"install"}),
        empty_is_manifest=True,
    ),
    # One-shot runners: no subcommand, arguments are packages.
    "npx": Manager(
        "npx", Ecosystem.NPM, is_runner=True, package_flags=_DLX_PACKAGE_FLAGS
    ),
    "pnpx": Manager(
        "pnpx", Ecosystem.NPM, is_runner=True, package_flags=_DLX_PACKAGE_FLAGS
    ),
    "uvx": Manager(
        "uvx",
        Ecosystem.PYPI,
        is_runner=True,
        package_flags=frozenset({"--from", "--with"}),
    ),
}

#: ``(binary, subcommand)`` pairs that behave as one-shot runners.
_RUNNER_SUBCOMMANDS: dict[tuple[str, str], Manager] = {
    ("pnpm", "dlx"): Manager(
        "pnpm dlx", Ecosystem.NPM, is_runner=True, package_flags=_DLX_PACKAGE_FLAGS
    ),
    ("yarn", "dlx"): Manager(
        "yarn dlx", Ecosystem.NPM, is_runner=True, package_flags=_DLX_PACKAGE_FLAGS
    ),
    ("bun", "x"): Manager(
        "bun x", Ecosystem.NPM, is_runner=True, package_flags=_DLX_PACKAGE_FLAGS
    ),
}

#: ``uv pip`` is a pip-compatible surface on the ``uv`` binary, so it is
#: addressed as a two-word manager rather than a subcommand of ``uv``.
_UV_PIP = Manager("uv pip", Ecosystem.PYPI)

#: Interpreters that can delegate via ``-m`` (``python3 -m pip install …``).
_PYTHON_BINARY = re.compile(r"^python(?:3(?:\.\d+)?)?$")
_PYTHON_MODULES = frozenset({"pip", "pipx", "uv"})


# ---------------------------------------------------------------------------
# 4. Shell tokenizing
# ---------------------------------------------------------------------------

#: Shell tokens that terminate one command and begin another.
_SEGMENT_SEPARATORS = frozenset({"&&", "||", ";", ";;", "|", "&", "(", ")"})

#: Redirection operators. ``shlex`` splits ``2>&1`` into ``2``, ``>&``, ``1``,
#: so these are the bare operators; a leading descriptor number is handled by
#: :func:`_drop_redirections`.
_REDIRECTIONS = frozenset(
    {">", ">>", "<", ">&", "<&", "&>", "&>>", ">|", "<<", "<<<", "<>"}
)

_FD_NUMBER = re.compile(r"^\d{1,2}$")

_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

#: Command prefixes to strip before identifying the manager.
_WRAPPERS = frozenset({"sudo", "command", "exec", "time", "nohup", "nice", "doas"})

#: Wrapper flags that consume a value (``sudo -u root npm i …``). Without
#: these, `-u` is read as the command name and the install goes undetected.
# fmt: off
_WRAPPER_VALUE_FLAGS = frozenset({
    "-u", "--user", "-g", "--group", "-U", "--other-user",
    "-p", "--prompt", "-C", "--close-from", "-D", "--chdir",
    "-R", "--chroot", "-r", "--role", "-t", "--type",
    "-n", "--adjustment",
})
# fmt: on

#: Executable suffixes to trim on Windows-style invocations.
_EXE_SUFFIXES = (".exe", ".cmd", ".bat")


def split_segments(command: str) -> list[list[str]]:
    """Tokenize ``command`` and split it into independent command segments.

    Uses :mod:`shlex` with ``punctuation_chars`` so shell operators arrive as
    their own tokens, then cuts on those operators. Quotes are removed, so
    ``npm install "left-pad"`` and ``npm install left-pad`` tokenize alike.

    Returns an empty list when the command cannot be tokenized at all.
    """
    tokens = _tokenize(command)
    if not tokens:
        return []

    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SEGMENT_SEPARATORS:
            if current:
                segments.append(current)
            current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _tokenize(command: str) -> list[str]:
    """Best-effort shell tokenization, tolerant of unbalanced quotes."""
    for posix in (True, False):
        lexer = shlex.shlex(command, posix=posix, punctuation_chars=True)
        lexer.whitespace_split = True
        try:
            tokens = list(lexer)
        except ValueError:
            continue  # unbalanced quotes — retry non-posix
        return tokens if posix else [token.strip("\"'") for token in tokens]
    return []


def _drop_redirections(args: list[str]) -> list[str]:
    """Strip shell redirections, including file-descriptor prefixes.

    ``shlex`` tokenizes ``2>&1`` as ``["2", ">&", "1"]``, so a naive pass reads
    ``2`` and ``1`` as package names. A bare descriptor number immediately
    preceding a redirection operator is dropped with the operator and target.
    """
    kept: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        following = args[index + 1] if index + 1 < len(args) else None

        if _FD_NUMBER.match(token) and following in _REDIRECTIONS:
            index += 3  # descriptor, operator, target
        elif token in _REDIRECTIONS:
            index += 2  # operator, target
        else:
            kept.append(token)
            index += 1
    return kept


def _strip_wrappers(tokens: list[str]) -> list[str]:
    """Drop ``sudo``, ``env``-style assignments, and similar prefixes.

    Once a wrapper has been seen, its own flags are stripped too — otherwise
    ``sudo -u root npm i lodash`` resolves ``-u`` as the command name and the
    install is never detected.
    """
    index = 0
    saw_wrapper = False
    while index < len(tokens):
        token = tokens[index]
        base = posixpath.basename(token)

        if base in _WRAPPERS:
            saw_wrapper = True
            index += 1
        elif _ENV_ASSIGNMENT.match(token):
            index += 1
        elif base == "env":
            saw_wrapper = True
            index += 1
            while index < len(tokens) and _ENV_ASSIGNMENT.match(tokens[index]):
                index += 1
        elif saw_wrapper and token.startswith("-"):
            flag, equals, _ = token.partition("=")
            index += 1
            if not equals and flag in _WRAPPER_VALUE_FLAGS and index < len(tokens):
                index += 1
        else:
            break
    return tokens[index:]


# ---------------------------------------------------------------------------
# 5. Identification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Invocation:
    """A segment resolved to a manager, its arguments, and its intent."""

    manager: Manager
    args: list[str]
    installs_manifest: bool = False


def _normalize_binary(token: str) -> str:
    base = posixpath.basename(token)
    for suffix in _EXE_SUFFIXES:
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def _identify(tokens: list[str]) -> _Invocation | None:
    """Resolve ``tokens`` to an :class:`_Invocation`, or ``None`` if not an install."""
    if not tokens:
        return None

    binary = _normalize_binary(tokens[0])
    rest = tokens[1:]

    # `python -m pip install …` delegates to the named module.
    if _PYTHON_BINARY.match(binary):
        if len(rest) >= 2 and rest[0] == "-m" and rest[1] in _PYTHON_MODULES:
            return _identify([rest[1], *rest[2:]])
        return None

    # `uv pip install …` / `uv pip sync` behave as pip.
    if binary == "uv" and len(rest) >= 2 and rest[0] == "pip":
        if rest[1] == "install":
            return _Invocation(_UV_PIP, rest[2:])
        if rest[1] == "sync":
            return _Invocation(_UV_PIP, rest[2:], installs_manifest=True)
        return None

    # Two-word runners: `pnpm dlx`, `yarn dlx`, `bun x`.
    if rest:
        runner = _RUNNER_SUBCOMMANDS.get((binary, rest[0]))
        if runner is not None:
            return _Invocation(runner, rest[1:])

    manager = MANAGERS.get(binary)
    if manager is None:
        return None

    if manager.is_runner:
        return _Invocation(manager, rest)

    if not rest:
        if manager.bare_is_manifest:
            return _Invocation(manager, [], installs_manifest=True)
        return None

    subcommand, arguments = rest[0], rest[1:]
    if subcommand in manager.install_subcommands:
        return _Invocation(manager, arguments)
    if subcommand in manager.manifest_subcommands:
        return _Invocation(manager, arguments, installs_manifest=True)
    return None


# ---------------------------------------------------------------------------
# 6. Target extraction
# ---------------------------------------------------------------------------

#: Prefixes marking a target with no registry metadata to check.
# fmt: off
_NON_REGISTRY_PREFIXES = (
    "./", "../", "/", "~/", ".\\", "..\\",
    "git+", "git:", "http://", "https://", "file:", "ssh://",
    "github:", "gitlab:", "bitbucket:", "gist:", "npm:",
    "link:", "workspace:", "portal:", "patch:", "exec:",
    "svn+", "hg+", "bzr+",
)
_ARCHIVE_SUFFIXES = (
    ".tgz", ".tar.gz", ".tar.bz2", ".tar", ".zip", ".whl", ".egg", ".tar.xz",
)
# fmt: on


def _is_non_registry_target(token: str) -> bool:
    """True when ``token`` names a path, URL, archive, or VCS source."""
    if token in {".", ".."}:
        return True
    lowered = token.lower()
    if lowered.startswith(_NON_REGISTRY_PREFIXES) or lowered.endswith(
        _ARCHIVE_SUFFIXES
    ):
        return True
    # A path separator means a path — except in an npm scope (`@scope/pkg`).
    return ("/" in token and not token.startswith("@")) or "\\" in token


def _extract_targets(
    invocation: _Invocation,
) -> tuple[list[str], bool]:
    """Pull package targets out of an argument list.

    Returns ``(targets, saw_manifest_flag)``. Flags are skipped; flags known
    to take a value also consume the following token, so a value such as a
    registry URL is never mistaken for a package.
    """
    manager = invocation.manager
    rules = ECOSYSTEMS[manager.ecosystem]
    args = _drop_redirections(invocation.args)

    targets: list[str] = []
    saw_manifest_flag = False

    index = 0
    while index < len(args):
        token = args[index]
        index += 1

        if token == "--":
            continue
        if not token.startswith("-"):
            targets.append(token)
            continue

        flag, equals, inline = token.partition("=")

        # A flag whose value is itself a package to vet (`npx -p pkg`).
        if flag in manager.package_flags:
            if equals:
                if inline:
                    targets.append(inline)
            elif index < len(args):
                targets.append(args[index])
                index += 1
            continue

        if flag in _MANIFEST_FLAGS:
            saw_manifest_flag = True
        if not equals and flag in rules.value_flags:
            index += 1  # this flag consumes the next token

    return targets, saw_manifest_flag


def _build_specs(
    targets: list[str], ecosystem: Ecosystem
) -> tuple[list[PackageSpec], list[str]]:
    """Validate ``targets``, returning ``(specs, skipped)``."""
    rules = ECOSYSTEMS[ecosystem]
    specs: list[PackageSpec] = []
    skipped: list[str] = []
    seen: set[tuple[str, str | None]] = set()

    for token in targets:
        if _is_non_registry_target(token):
            skipped.append(token)
            continue

        name, version = rules.split_target(token)
        if not name or not rules.name_pattern.match(name):
            skipped.append(token)
            continue

        key = (name.lower(), version)
        if key in seen:
            continue
        seen.add(key)
        specs.append(
            PackageSpec(name=name, ecosystem=ecosystem, version_spec=version, raw=token)
        )

    return specs, skipped


# ---------------------------------------------------------------------------
# 7. Entry point
# ---------------------------------------------------------------------------


def parse_command(command: str) -> list[InstallCommand]:
    """Find every package install inside a shell ``command``.

    Returns one :class:`InstallCommand` per install invocation, in the order
    they appear. A command with no installs yields an empty list.

    >>> [c.manager for c in parse_command("cd app && npm i -D vitest")]
    ['npm']
    >>> parse_command("npm i -D vitest")[0].specs[0].name
    'vitest'
    """
    if not command or not command.strip():
        return []

    found: list[InstallCommand] = []
    for tokens in split_segments(command):
        invocation = _identify(_strip_wrappers(tokens))
        if invocation is None:
            continue

        manager = invocation.manager
        targets, saw_manifest_flag = _extract_targets(invocation)
        specs, skipped = _build_specs(targets, manager.ecosystem)

        # A command naming a target we cannot vet (`npm i ./local`) is not a
        # manifest install; reporting it as one alongside a skip contradicts
        # itself. So key this on `targets`, not on `specs`.
        installs_manifest = (
            invocation.installs_manifest
            or saw_manifest_flag
            or (manager.empty_is_manifest and not targets)
        )

        if not specs and not installs_manifest and not skipped:
            continue

        found.append(
            InstallCommand(
                manager=manager.binary,
                ecosystem=manager.ecosystem,
                specs=specs,
                raw=" ".join(tokens),
                installs_manifest=installs_manifest,
                skipped=skipped,
            )
        )
    return found
