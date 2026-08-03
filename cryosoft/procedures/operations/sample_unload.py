"""SampleUnloadOperation — bring the cryostat to a safe state to unload a sample."""

from __future__ import annotations

from cryosoft.procedures.operations.sample_access_base import _SampleAccessOperationBase


class SampleUnloadOperation(_SampleAccessOperationBase):
    """Verify the cryostat is safe to open so the sample can be unloaded.

    All behavior is inherited from ``_SampleAccessOperationBase`` — see its
    docstring. This class exists to give the "unload" half of the
    sample-access pair its own display name, config key,
    servicing-log ``entry_kind`` (derived from ``name`` by
    ``CryogenicsRecorder``), and rod-step wording, distinct from
    ``SampleLoadOperation``'s.
    """

    name = "Sample Unload"
    description = "Bring the cryostat to a safe state to unload the sample"
    ready_message = "Ready — sample can be unloaded"
    config_key = "sample_unload"
    rod_step_label = "Withdraw the sample rod"
