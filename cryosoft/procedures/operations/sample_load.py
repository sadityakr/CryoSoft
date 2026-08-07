"""SampleLoadOperation — bring the cryostat to a safe state to load a sample."""

from __future__ import annotations

from cryosoft.procedures.operations.sample_access_base import _SampleAccessOperationBase


class SampleLoadOperation(_SampleAccessOperationBase):
    """Verify the cryostat is safe to open so a sample can be loaded.

    All behavior is inherited from ``_SampleAccessOperationBase`` — see its
    docstring. This class exists to give the "load" half of the sample-access
    pair its own display name, config key, and servicing-log ``entry_kind``
    (derived from ``name`` by ``CryogenicsRecorder``), distinct from
    ``SampleUnloadOperation``'s.
    """

    name = "Sample Load"
    description = "Bring the cryostat to a safe state to load a sample"
    ready_message = "Ready — sample can be loaded"
    config_key = "sample_load"
