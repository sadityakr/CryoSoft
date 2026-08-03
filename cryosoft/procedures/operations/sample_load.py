# ---
# description: |
#   SampleLoadOperation: the "load a sample" half of the sample-access pair
#   (see sample_access_base.py's _SampleAccessOperationBase, which implements
#   everything). Declares only this operation's identity — name, description,
#   ready_message, config_key — so the Operations panel's generic
#   config-block discovery and card-building can find it.
# entry_point: Not run directly. Constructed by the GUI's Operations panel
#   or a test, submitted via Orchestrator.run_operation()/queue_operation().
# dependencies:
#   - cryosoft.procedures.operations.sample_access_base (_SampleAccessOperationBase)
# input: |
#   See _SampleAccessOperationBase — operations.sample_load: config block.
# process: |
#   Entirely inherited from _SampleAccessOperationBase.
# output: |
#   See _SampleAccessOperationBase. The run manifest's "procedure" name
#   ("Sample Load") makes CryogenicsRecorder derive entry_kind="sample_load"
#   for this run's servicing-log entry.
# last_updated: 2026-07-27
# ---

"""SampleLoadOperation — bring the cryostat to a safe state to load a sample."""

from __future__ import annotations

from cryosoft.procedures.operations.sample_access_base import _SampleAccessOperationBase


class SampleLoadOperation(_SampleAccessOperationBase):
    """Verify the cryostat is safe to open so a sample can be loaded.

    All behavior is inherited from ``_SampleAccessOperationBase`` — see its
    docstring. This class exists to give the "load" half of the sample-access
    pair its own display name, config key, servicing-log ``entry_kind``
    (derived from ``name`` by ``CryogenicsRecorder``), and rod-step
    wording, distinct from ``SampleUnloadOperation``'s.
    """

    name = "Sample Load"
    description = "Bring the cryostat to a safe state to load a sample"
    ready_message = "Ready — sample can be loaded"
    config_key = "sample_load"
    rod_step_label = "Insert the sample rod"
