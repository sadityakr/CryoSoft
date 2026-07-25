"""Operations sub-package: concrete OperationBase subclasses.

Servicing actions (helium fill, sample change), as distinct from measurement
procedures (``cryosoft.procedures``). Discovered by
``tests/test_conformance.py``'s ``_all_operation_classes()`` walk of
``cryosoft.procedures``, and by the GUI's ``discover_operations()`` — never
by ``discover_procedures()``.
"""
