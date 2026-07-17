# ---
# description: |
#   cryosoft.session — the L6 Session Management layer. Manages complete
#   experiments: who is measuring (User), which sample under which
#   per-experiment safety envelope (ExperimentRecord + SessionEnvelope), and
#   which runs were produced (RunRecord, recorded automatically from the
#   Orchestrator's run manifests). Sits between core and the GUI: imported by
#   gui/main, imports Orchestrator/Station downward, guarded by import-linter
#   contracts C11/C12.
# entry_point: Not run directly. Constructed in cryosoft.main and injected into
#   the GUI (like the ConfigCatalog).
# dependencies:
#   - cryosoft.core.orchestrator (run manifests, session envelope)
#   - cryosoft.core.plan (SessionEnvelope / EnvelopeBound)
# ---

"""cryosoft.session — the L6 Session Management layer.

See ``cryosoft/session/README.md`` for the layer standard and
``docs/plans/session-management-layer.md`` / ``agent-native-architecture.md``
for the design this implements.
"""

from cryosoft.session.manager import SessionManager
from cryosoft.session.models import ElnLink, ExperimentRecord, RunRecord, User
from cryosoft.session.store import ExperimentStore, UserRoster

__all__ = [
    "SessionManager",
    "ExperimentStore",
    "UserRoster",
    "ExperimentRecord",
    "RunRecord",
    "User",
    "ElnLink",
]
