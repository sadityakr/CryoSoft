# ---
# description: |
#   SampleUnloadOperation: the "unload a sample" half of the sample-access
#   pair (see sample_access_base.py's _SampleAccessOperationBase, which
#   implements everything). Declares only this operation's identity — name,
#   description, ready_message, config_key — so the Operations panel's
#   generic config-block discovery and card-building can find it.
# entry_point: Not run directly. Constructed by the GUI's Operations panel
#   or a test, submitted via Orchestrator.run_operation()/queue_operation().
# dependencies:
#   - cryosoft.procedures.operations.sample_access_base (_SampleAccessOperationBase)
# input: |
#   See _SampleAccessOperationBase — operations.sample_unload: config block.
# process: |
#   Entirely inherited from _SampleAccessOperationBase.
# output: |
#   See _SampleAccessOperationBase. The run manifest's "procedure" name
#   ("Sample Unload") makes CryogenicsRecorder derive
#   entry_kind="sample_unload" for this run's servicing-log entry.
# last_updated: 2026-07-27
# ---

"""SampleUnloadOperation — bring the cryostat to a safe state to unload a sample."""

from __future__ import annotations

from cryosoft.procedures.operations.sample_access_base import _SampleAccessOperationBase


class SampleUnloadOperation(_SampleAccessOperationBase):
    """Verify the cryostat is safe to open so the sample can be unloaded.

    All behavior is inherited from ``_SampleAccessOperationBase`` — see its
    docstring. This class exists to give the "unload" half of the
    sample-access pair its own display name, config key, and
    servicing-log ``entry_kind`` (derived from ``name`` by
    ``CryogenicsRecorder``), distinct from ``SampleLoadOperation``'s.
    """

    name = "Sample Unload"
    description = "Bring the cryostat to a safe state to unload the sample"
    ready_message = "Ready — sample can be unloaded"
    config_key = "sample_unload"
