# ---
# description: |
#   cryosoft.session — the L6 Session Management layer. Manages complete
#   experiments: who is measuring (User), which sample under which
#   per-experiment safety envelope (ExperimentRecord + SessionEnvelope), and
#   which runs were produced (RunRecord, recorded automatically from the
#   Orchestrator's run manifests). Also hosts the Servicing Log framework
#   (servicing_log.py): declared log kinds, revisioned per-kind storage, the
#   hourly helium record, and the automatic CryogenicsRecorder writer. Sits
#   between core and the GUI: imported by gui/main, imports
#   Orchestrator/Station downward, guarded by import-linter contracts C11/C12.
# entry_point: Not run directly. Constructed in cryosoft.main and injected into
#   the GUI (like the ConfigCatalog).
# dependencies:
#   - cryosoft.core.orchestrator (run manifests, session envelope)
#   - cryosoft.core.plan (SessionEnvelope / EnvelopeBound, ParamSpec)
# ---

"""cryosoft.session — the L6 Session Management layer.

See ``cryosoft/session/README.md`` for the layer standard,
``docs/plans/session-management-layer.md`` / ``agent-native-architecture.md``
for the experiment-management design, and
``docs/plans/cryogenics-logbook.md`` for the Servicing Log framework.
"""

from cryosoft.session.manager import SessionManager
from cryosoft.session.models import ElnLink, ExperimentRecord, RunRecord, ServiceLogEntry, User
from cryosoft.session.servicing_log import (
    DECLARED_LOG_KINDS,
    CryogenicsRecorder,
    HeliumRecordStore,
    LogKindSpec,
    ServicingLogStore,
    consumption_rate_pct_per_h,
)
from cryosoft.session.store import ExperimentStore, UserRoster

__all__ = [
    "SessionManager",
    "ExperimentStore",
    "UserRoster",
    "ExperimentRecord",
    "RunRecord",
    "User",
    "ElnLink",
    "ServiceLogEntry",
    "LogKindSpec",
    "DECLARED_LOG_KINDS",
    "ServicingLogStore",
    "HeliumRecordStore",
    "consumption_rate_pct_per_h",
    "CryogenicsRecorder",
]
