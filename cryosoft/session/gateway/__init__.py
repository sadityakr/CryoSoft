"""cryosoft.session.gateway — the Agent gateway (L6).

The second adapter of the **Control contract**: an in-process client that
speaks the same ``Command`` / ``Verdict`` / ``Event`` vocabulary the GUI
does, with a permission model in front of it. See
``cryosoft/session/gateway/README.md`` for the folder standard, ``roles.py``
for the permission standard itself, ``action_classes.py`` for the
(provisional) classification of every action, and ``GLOSSARY.md`` for the
**Role** / **Action class** / **Attendance** / **Kill switch** / **Agent
gateway** vocabulary.
"""

from cryosoft.session.gateway.action_classes import (
    COMMAND_ACTION_CLASSES,
    CONTROL_ACTION_CLASSES,
    LIFECYCLE_ACTION_CLASSES,
    ActionClass,
    ClassifiedAction,
    UnclassifiedActionError,
    classify_command,
    classify_control,
)
from cryosoft.session.gateway.gateway import EngineClient, Gateway
from cryosoft.session.gateway.roles import (
    PERMISSION_MATRIX,
    Permission,
    Role,
    authorize,
)

__all__ = [
    "ActionClass",
    "ClassifiedAction",
    "UnclassifiedActionError",
    "COMMAND_ACTION_CLASSES",
    "CONTROL_ACTION_CLASSES",
    "LIFECYCLE_ACTION_CLASSES",
    "classify_command",
    "classify_control",
    "Role",
    "Permission",
    "PERMISSION_MATRIX",
    "authorize",
    "Gateway",
    "EngineClient",
]
