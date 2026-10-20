"""installguard — vet packages for supply-chain red flags before they install.

Runs as a Claude Code ``PreToolUse`` hook that inspects ``Bash`` commands for
package installs, or as a standalone CLI. Stdlib only, no runtime dependencies.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
