"""Tests for install-command parsing."""

from __future__ import annotations

import pytest

from installguard.parse import Ecosystem, parse_command


def names(command: str) -> list[str]:
    """All package names parsed out of ``command``, across every segment."""
    return [spec.name for cmd in parse_command(command) for spec in cmd.specs]


def only(command: str):
    """The single InstallCommand in ``command``; fails if there isn't exactly one."""
    commands = parse_command(command)
    assert len(commands) == 1, f"expected 1 install command, got {len(commands)}"
    return commands[0]


# --------------------------------------------------------------------------
# npm family
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "command",
    [
        "npm install express",
        "npm i express",
        "npm add express",
        "npm install --save express",
        "npm install express --save-dev",
        "pnpm add express",
        "pnpm i express",
        "yarn add express",
        "bun add express",
    ],
)
def test_npm_family_basic(command: str) -> None:
    assert names(command) == ["express"]
    assert only(command).ecosystem is Ecosystem.NPM


def test_npm_multiple_packages() -> None:
    assert names("npm install react react-dom classnames") == [
        "react",
        "react-dom",
        "classnames",
    ]


def test_npm_scoped_package() -> None:
    assert names("npm i @types/node") == ["@types/node"]


def test_npm_scoped_with_version() -> None:
    spec = only("npm i @types/node@20.1.0").specs[0]
    assert spec.name == "@types/node"
    assert spec.version_spec == "20.1.0"


def test_npm_version_ranges() -> None:
    spec = only("npm i express@^4.18.0").specs[0]
    assert (spec.name, spec.version_spec) == ("express", "^4.18.0")


def test_npm_dist_tag() -> None:
    spec = only("npm i react@next").specs[0]
    assert (spec.name, spec.version_spec) == ("react", "next")


def test_npm_boolean_flags_are_not_packages() -> None:
    assert names("npm i -g -D -E --no-save --legacy-peer-deps typescript") == [
        "typescript"
    ]


def test_npm_value_flag_consumes_next_token() -> None:
    # `private-pkg` must not be read as the value of --registry.
    assert names("npm i --registry https://r.example.com private-pkg") == [
        "private-pkg"
    ]


def test_npm_value_flag_equals_form() -> None:
    assert names("npm i --registry=https://r.example.com private-pkg") == [
        "private-pkg"
    ]


def test_npm_workspace_flag() -> None:
    assert names("npm i -w packages/app lodash") == ["lodash"]


# --------------------------------------------------------------------------
# Manifest installs (no named packages)
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "command",
    [
        "npm install",
        "npm ci",
        "npm i",
        "yarn",
        "yarn install",
        "pnpm install",
        "bun install",
    ],
)
def test_manifest_installs(command: str) -> None:
    cmd = only(command)
    assert cmd.installs_manifest is True
    assert cmd.specs == []


def test_pip_requirements_file_is_manifest() -> None:
    cmd = only("pip install -r requirements.txt")
    assert cmd.installs_manifest is True
    assert cmd.specs == []


# --------------------------------------------------------------------------
# PyPI family
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "command",
    [
        "pip install requests",
        "pip3 install requests",
        "python -m pip install requests",
        "python3 -m pip install requests",
        "python3.12 -m pip install requests",
        "uv pip install requests",
        "uv add requests",
        "poetry add requests",
        "pipx install requests",
    ],
)
def test_pypi_family_basic(command: str) -> None:
    assert names(command) == ["requests"]
    assert only(command).ecosystem is Ecosystem.PYPI


def test_pip_pinned_version() -> None:
    spec = only("pip install requests==2.31.0").specs[0]
    assert (spec.name, spec.version_spec) == ("requests", "==2.31.0")


def test_pip_version_range() -> None:
    spec = only("pip install 'django>=4.2,<5'").specs[0]
    assert spec.name == "django"
    assert spec.version_spec == ">=4.2,<5"


def test_pip_extras_stripped_from_name() -> None:
    spec = only("pip install requests[socks]").specs[0]
    assert spec.name == "requests"


def test_pip_extras_with_version() -> None:
    spec = only("pip install 'requests[socks]>=2.0'").specs[0]
    assert (spec.name, spec.version_spec) == ("requests", ">=2.0")


def test_pip_environment_marker_dropped() -> None:
    spec = only("pip install 'tomli; python_version < \"3.11\"'").specs[0]
    assert spec.name == "tomli"


def test_pip_index_url_not_a_package() -> None:
    assert names("pip install -i https://pypi.example.com/simple internal-lib") == [
        "internal-lib"
    ]


def test_pip_upgrade_flag() -> None:
    assert names("pip install --upgrade urllib3") == ["urllib3"]


# --------------------------------------------------------------------------
# Targets we cannot vet
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "command",
    [
        "pip install -e .",
        "pip install ./local-pkg",
        "pip install /abs/path/pkg",
        "npm i ./local-module",
        "npm i ../sibling",
        "npm i git+https://github.com/o/r.git",
        "npm i https://example.com/pkg.tgz",
        "pip install git+https://github.com/o/r.git",
        "pip install ./dist/pkg-1.0-py3-none-any.whl",
    ],
)
def test_non_registry_targets_yield_no_specs(command: str) -> None:
    for cmd in parse_command(command):
        assert cmd.specs == [], f"{command} should produce no vettable specs"


def test_skipped_targets_are_recorded() -> None:
    cmd = only("npm i git+https://github.com/o/r.git")
    assert cmd.skipped == ["git+https://github.com/o/r.git"]


def test_local_path_alongside_real_package() -> None:
    cmd = only("pip install ./local-pkg requests")
    assert [s.name for s in cmd.specs] == ["requests"]
    assert cmd.skipped == ["./local-pkg"]


# --------------------------------------------------------------------------
# Shell structure
# --------------------------------------------------------------------------

def test_chained_with_and() -> None:
    assert names("cd app && npm install lodash") == ["lodash"]


def test_two_installs_in_one_chain() -> None:
    commands = parse_command("npm i react && pip install requests")
    assert len(commands) == 2
    assert commands[0].ecosystem is Ecosystem.NPM
    assert commands[1].ecosystem is Ecosystem.PYPI


def test_semicolon_separated() -> None:
    assert names("echo hi; npm i axios") == ["axios"]


def test_piped_command() -> None:
    assert names("cat x | grep y") == []


def test_sudo_prefix() -> None:
    assert names("sudo pip install requests") == ["requests"]


def test_env_assignment_prefix() -> None:
    assert names("NODE_ENV=production npm i express") == ["express"]


def test_env_command_prefix() -> None:
    assert names("env FOO=1 BAR=2 pip install requests") == ["requests"]


def test_quoted_package_name() -> None:
    assert names('npm install "left-pad"') == ["left-pad"]


def test_redirection_target_not_a_package() -> None:
    assert names("npm i lodash > out.log") == ["lodash"]


def test_deduplicates_repeated_package() -> None:
    assert names("npm i lodash lodash") == ["lodash"]


# --------------------------------------------------------------------------
# Execute-without-install runners
# --------------------------------------------------------------------------

def test_npx_is_vetted() -> None:
    assert names("npx create-react-app") == ["create-react-app"]
    assert only("npx create-react-app").manager == "npx"


def test_pnpm_dlx() -> None:
    assert names("pnpm dlx create-vite") == ["create-vite"]


def test_uvx() -> None:
    cmd = only("uvx ruff")
    assert cmd.specs[0].name == "ruff"
    assert cmd.ecosystem is Ecosystem.PYPI


# --------------------------------------------------------------------------
# Non-installs must not be detected
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "git status",
        "npm run build",
        "npm test",
        "npm publish",
        "npm uninstall lodash",
        "pip list",
        "pip uninstall requests",
        "yarn build",
        "python script.py",
        "poetry lock",
        "",
        "   ",
    ],
)
def test_no_false_positives(command: str) -> None:
    assert parse_command(command) == []


def test_unbalanced_quote_does_not_crash() -> None:
    # Must not raise; returning nothing or a best guess are both acceptable.
    parse_command('npm install "unterminated')


# --------------------------------------------------------------------------
# Regressions. Each of these shipped as a bug; the first three failed *open*,
# silently vetting nothing, which is the worst outcome for this tool.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("flag", ["-f", "--force", "-t", "-g", "-D", "-E", "-S"])
def test_npm_boolean_flags_do_not_swallow_package(flag: str) -> None:
    """`-f`/`-t` are boolean for npm but value-taking for pip.

    A shared flag table consumed the package name here, so `npm i -f lodash`
    reported zero packages and the install proceeded unvetted.
    """
    assert names(f"npm i {flag} lodash") == ["lodash"]


@pytest.mark.parametrize("flag", ["-f", "--find-links", "-t", "--target"])
def test_pip_value_flags_still_consume_their_value(flag: str) -> None:
    """The same flags must keep consuming values for pip — the other half."""
    assert names(f"pip install {flag} ./wheels requests") == ["requests"]


@pytest.mark.parametrize(
    "alias", ["it", "install-test", "cit", "install-ci-test", "isntall", "inst"]
)
def test_npm_install_aliases(alias: str) -> None:
    """npm's install aliases were unrecognized, so the install went undetected."""
    assert names(f"npm {alias} lodash") == ["lodash"]


def test_npx_package_flag_is_vetted() -> None:
    """`npx -p typescript tsc` downloads and runs typescript — vet it."""
    assert "typescript" in names("npx -p typescript tsc")


def test_npx_package_flag_equals_form() -> None:
    assert names("npx --package=esbuild esbuild") == ["esbuild"]


def test_uvx_from_flag_is_vetted() -> None:
    assert "httpie" in names("uvx --from httpie http")


@pytest.mark.parametrize(
    "command",
    [
        "npm i lodash 2>&1",
        "npm i lodash >out.log 2>&1",
        "npm i lodash 2>/dev/null",
        "pip install requests 2>&1",
    ],
)
def test_file_descriptors_are_not_packages(command: str) -> None:
    """shlex splits `2>&1` into `2`, `>&`, `1` — `2` and `1` looked like names."""
    expected = ["lodash"] if "npm" in command else ["requests"]
    assert names(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        "sudo -u root npm i lodash",
        "sudo --user=root npm i lodash",
        "sudo -H -u root npm i lodash",
        "nice -n 10 npm i lodash",
        "sudo -E npm i lodash",
    ],
)
def test_wrapper_flags_do_not_hide_the_install(command: str) -> None:
    """Wrapper flags were read as the command name, hiding the install."""
    assert names(command) == ["lodash"]


def test_unvettable_target_is_not_a_manifest_install() -> None:
    """`npm i ./local` named a target, so it is not a manifest install."""
    cmd = only("npm i ./local")
    assert cmd.installs_manifest is False
    assert cmd.skipped == ["./local"]


def test_display_property() -> None:
    spec = only("npm i express@4.18.0").specs[0]
    assert spec.display == "express@4.18.0"
    bare = only("npm i express").specs[0]
    assert bare.display == "express"
