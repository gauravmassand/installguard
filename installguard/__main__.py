"""Allow ``python -m installguard`` to work the same as the console script."""

from .cli import main

raise SystemExit(main())
