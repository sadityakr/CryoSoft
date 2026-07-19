"""Operations sub-package: concrete OperationBase subclasses.

Servicing actions (helium fill, sample change — see
``docs/plans/cryogenics-logbook.md``), as distinct from measurement
procedures (``cryosoft.procedures``). Discovered by
``tests/test_conformance.py``'s ``_all_operation_classes()`` walk of
``cryosoft.procedures``, and by the GUI's future ``discover_operations()``
(plan §4.1) — never by ``discover_procedures()``.
"""
