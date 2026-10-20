"""Allow ``python -m pkgpatrol`` to work the same as the console script."""

from .cli import main

raise SystemExit(main())
