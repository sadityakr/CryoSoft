"""Procedure/operation auto-discovery for the GUI."""

from __future__ import annotations

import importlib
import logging
import pkgutil
from pathlib import Path

from cryosoft.core.operation import OperationBase
from cryosoft.core.procedure import BaseProcedure

logger = logging.getLogger(__name__)


def all_subclasses(cls: type) -> list[type]:
    """Return every transitive subclass of *cls* (depth-first, deduplicated).

    ``type.__subclasses__()`` lists only *direct* subclasses, so it would miss a
    concrete procedure sitting under an intermediate base such as
    ``SweepMeasureProcedure``. This walks the whole tree.

    Args:
        cls: The base class whose subclass tree is walked.

    Returns:
        All transitive subclasses, in depth-first discovery order.
    """
    result: list[type] = []
    seen: set[type] = set()
    for sub in cls.__subclasses__():
        if sub not in seen:
            seen.add(sub)
            result.append(sub)
        result.extend(all_subclasses(sub))
    return result


def discover_procedures() -> list[type[BaseProcedure]]:
    """Import all modules in cryosoft.procedures and return concrete procedures.

    Returns every named ``BaseProcedure`` subclass at any depth (so a procedure
    under an intermediate base like ``SweepMeasureProcedure`` is found), skipping
    unnamed intermediate bases.

    Returns:
        List of concrete BaseProcedure subclasses (not the base or intermediate
        bases, which carry no ``name``).
    """
    import cryosoft.procedures as _pkg

    pkg_path = Path(_pkg.__file__).parent
    for _, module_name, _ in pkgutil.iter_modules([str(pkg_path)]):
        try:
            importlib.import_module(f"cryosoft.procedures.{module_name}")
        except Exception:
            logger.exception("procedure_discovery: failed to import cryosoft.procedures.%s", module_name)

    subclasses: list[type[BaseProcedure]] = []
    seen: set[type] = set()
    for cls in all_subclasses(BaseProcedure):
        if getattr(cls, "name", "") and cls not in seen:
            seen.add(cls)
            subclasses.append(cls)
    return subclasses


def discover_operations() -> list[type[OperationBase]]:
    """Import all modules in cryosoft.procedures.operations and return concrete operations.

    Same pkgutil-walk-and-import pattern as ``discover_procedures()``, over
    the operations subpackage instead. The Operations panel uses
    this to build one card per discovered class whose ``config_key`` matches
    a key in the ``operations:`` config block — no per-operation GUI code.

    Returns:
        List of concrete ``OperationBase`` subclasses (not the base, which
        carries no ``name``).
    """
    import cryosoft.procedures.operations as _pkg

    pkg_path = Path(_pkg.__file__).parent
    for _, module_name, _ in pkgutil.iter_modules([str(pkg_path)]):
        try:
            importlib.import_module(f"cryosoft.procedures.operations.{module_name}")
        except Exception:
            logger.exception(
                "procedure_discovery: failed to import cryosoft.procedures.operations.%s",
                module_name,
            )

    subclasses: list[type[OperationBase]] = []
    seen: set[type] = set()
    for cls in all_subclasses(OperationBase):
        if getattr(cls, "name", "") and cls not in seen:
            seen.add(cls)
            subclasses.append(cls)
    return subclasses
