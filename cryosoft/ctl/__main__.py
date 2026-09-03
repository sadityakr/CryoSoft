"""Entry point: ``python -m cryosoft.ctl [connection options] <subcommand>``."""

import sys

from cryosoft.ctl.cli import main

sys.exit(main())
