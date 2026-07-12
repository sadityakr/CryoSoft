"""Entry point: ``python -m cryosoft.troubleshoot <subcommand> ...``."""

import sys

from cryosoft.troubleshoot.cli import main

sys.exit(main())
