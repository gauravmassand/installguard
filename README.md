# installguard

Vet npm and PyPI packages for supply-chain red flags **before** they install.

installguard hooks into Claude Code's `PreToolUse` event, watches for package
installs in `Bash` commands, and checks each package against the npm registry,
PyPI, and [deps.dev](https://deps.dev) — surfacing known advisories,
suspiciously new or unpopular packages, and likely typosquats. The intent is
that installs with known CVEs are denied even when Claude Code is running in
auto-approve mode.

It also works as a plain CLI, so you can check a package by hand.

## Status

**Early development.** The command parser is done and tested; the registry
clients, report, and hook integration are not built yet.

| Command | What it does | State |
| --- | --- | --- |
| `installguard parse '<cmd>'` | Show what the parser extracts from a shell command. No network calls. | **Working** |
| `installguard check <pkg>` | Vet named packages against the registries. | Not implemented |
| `installguard hook` | Read a `PreToolUse` payload on stdin and allow/deny. | Not implemented |
| `installguard init` | Write hook config into `~/.claude/settings.json`. | Not implemented |

Unimplemented subcommands exit non-zero with a clear message. They do not
silently succeed — a security tool that appears to run but checks nothing is
worse than one that admits it is unfinished.

## Install

Not on PyPI yet. From a clone:

```sh
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

Requires Python 3.10 or newer.

## Try the parser

```console
$ installguard parse 'cd app && npm i -D @types/node@20 lodash && pip install -r reqs.txt httpx'
npm (npm)
  package  @types/node@20
  package  lodash
pip (pypi)
  package  httpx
  manifest install (package.json / requirements.txt)
```

It understands npm/yarn/pnpm/bun, pip/pip3/uv/poetry/pipx, the one-shot
runners (`npx`, `uvx`, `pnpm dlx`), `python -m pip` delegation, `sudo` and
`VAR=x` prefixes, `&&`/`;`/`|` chains, redirections, and scoped or pinned
names. Targets with no registry metadata — local paths, git URLs, tarballs —
are reported as skipped rather than silently dropped.

## Development

```sh
.venv/bin/python -m pytest tests/ -q
```

## Design notes

- **Zero runtime dependencies.** Standard library only. A security tool that
  installs a dependency tree to check your dependency tree is self-defeating,
  and a hook that fails to import is a hook that silently stops guarding you.
  CI asserts this on every run.
- **Fail open, not closed.** Network timeouts and registry outages should let
  the install proceed with a warning. A broken check must never wedge your
  workflow. Known CVEs are the intended exception.
- **Over-detect rather than under-detect.** Where a command token is
  ambiguous, treat it as a package and let the registry decide. A false
  positive costs one wasted lookup; a false negative means an unvetted package
  on disk.
- **Per-ecosystem flag tables.** `-f` is `--find-links` for pip but `--force`
  for npm. Sharing one table across ecosystems made `npm i -f lodash` report
  zero packages — a failure that passes silently. The parser keeps them
  separate, with regression tests.

## License

MIT — see [LICENSE](LICENSE).
