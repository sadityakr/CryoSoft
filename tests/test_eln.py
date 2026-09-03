"""Behaviour tests for the ELN track (cryosoft/session/eln/).

Conformance (tests/test_conformance.py) checks that every adapter *matches the
standard*; these tests check that the track actually publishes, queues,
retries, and records what it should. Everything runs against the in-memory
``SimElnAdapter`` and, for the eLabFTW backend, a fake transport — no network,
ever.
"""

from __future__ import annotations

import json

import pytest

from cryosoft.session.eln.adapter import ElnEntryRef, ElnError
from cryosoft.session.eln.outbox import (
    DRAIN_IDLE,
    DRAIN_PUBLISHED,
    DRAIN_RETRY,
    Outbox,
    OutboxJob,
)
from cryosoft.session.eln.settings import (
    API_KEY_ENV_VAR,
    SETTINGS_PATH_ENV_VAR,
    ElnSettings,
    eln_settings_path,
    load_eln_settings,
)
from cryosoft.session.eln.sim_eln import SimElnAdapter
from cryosoft.session.eln.templates import (
    render_run_body,
    render_run_metadata,
    render_run_title,
)

MANIFEST = {
    "run_id": "run-0001",
    "procedure": "Field Sweep",
    "kind": "run",
    "params": {"field_T": 1.5, "rate_T_per_s": 0.01},
    "data_file": "data/run-0001.h5",
    "started_utc": "2026-01-01T10:00:00+00:00",
    "finished_utc": "2026-01-01T11:00:00+00:00",
    "status": "done",
    "reason": "",
}


# ── User-level settings ───────────────────────────────────────────────────────


def test_settings_path_prefers_the_environment_override(monkeypatch, tmp_path):
    """``CRYOSOFT_ELN_SETTINGS`` wins over the per-user application-data path."""
    monkeypatch.setenv(SETTINGS_PATH_ENV_VAR, str(tmp_path / "custom.json"))
    assert eln_settings_path() == tmp_path / "custom.json"


def test_settings_path_is_user_level_not_shipped_config(monkeypatch):
    """With no override the file lives in the per-user application-data dir."""
    monkeypatch.delenv(SETTINGS_PATH_ENV_VAR, raising=False)
    path = eln_settings_path()
    assert path.name == "eln-settings.json"
    assert "configs" not in path.parts, "an API key must never live in a shipped config"


def test_missing_settings_file_leaves_publishing_off(monkeypatch, tmp_path):
    """A missing file is the normal case: disabled defaults, no exception."""
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
    settings = load_eln_settings(tmp_path / "nope.json")
    assert settings == ElnSettings()
    assert not settings.enabled and not settings.is_configured


def test_malformed_settings_file_degrades_to_defaults(monkeypatch, tmp_path):
    """Junk on disk switches publishing off rather than taking the app down."""
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
    path = tmp_path / "eln-settings.json"
    path.write_text("{not json at all", encoding="utf-8")
    assert load_eln_settings(path) == ElnSettings()


def test_settings_round_trip_and_env_key_override(monkeypatch, tmp_path):
    """The file is read, and the environment key overrides its ``api_key``."""
    path = tmp_path / "eln-settings.json"
    written = ElnSettings(
        enabled=True, base_url="https://elab.example.org/", api_key="file-key"
    )
    path.write_text(json.dumps(written.to_dict(include_secret=True)), encoding="utf-8")

    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
    from_file = load_eln_settings(path)
    assert from_file.api_key == "file-key"
    assert from_file.base_url == "https://elab.example.org", "trailing slash trimmed"
    assert from_file.is_configured

    monkeypatch.setenv(API_KEY_ENV_VAR, "env-key")
    assert load_eln_settings(path).api_key == "env-key"


def test_api_key_never_appears_in_repr_or_a_plain_dict():
    """The key is redacted everywhere except an explicit include_secret dump."""
    settings = ElnSettings(enabled=True, base_url="https://x", api_key="s3cret")
    assert "s3cret" not in repr(settings)
    assert "s3cret" not in json.dumps(settings.to_dict())
    assert settings.to_dict(include_secret=True)["api_key"] == "s3cret"


def test_redacted_key_read_back_is_treated_as_no_key():
    """A settings dump written back without the secret does not resurrect it."""
    settings = ElnSettings(enabled=True, base_url="https://x", api_key="s3cret")
    assert ElnSettings.from_dict(settings.to_dict()).api_key == ""


# ── The sim adapter (the twin every other test builds on) ─────────────────────


def test_sim_adapter_creates_updates_and_attaches():
    """A healthy notebook records every accepted call."""
    adapter = SimElnAdapter({"base_url": "https://sim.example"})
    assert adapter.verify()
    assert adapter.list_templates()

    ref = adapter.create_entry("Title", None, "<p>body</p>", ["cryosoft"], {"k": "v"})
    assert ref.backend == "sim_eln"
    assert ref.entry_id in adapter.entries
    assert ref.url.endswith(ref.entry_id)

    adapter.update_entry(ref, "<p>new</p>", {"k": "w"})
    assert adapter.entries[ref.entry_id]["body_html"] == "<p>new</p>"

    adapter.attach_link(ref, "/data/run-0001.h5", "raw data")
    assert adapter.links == [
        {"entry_id": ref.entry_id, "url": "/data/run-0001.h5", "comment": "raw data"}
    ]


def test_sim_adapter_attaches_a_real_file_only(tmp_path):
    """``attach_file`` models the real backend's "the file must exist" failure."""
    adapter = SimElnAdapter({})
    ref = adapter.create_entry("T", None, "", [], {})
    data = tmp_path / "run.h5"
    data.write_bytes(b"\x89HDF\r\n\x1a\n")
    adapter.attach_file(ref, data, "raw data")
    assert adapter.uploads[0]["path"] == str(data)
    with pytest.raises(ElnError):
        adapter.attach_file(ref, tmp_path / "missing.h5")


def test_sim_adapter_models_offline_and_transient_failure():
    """Offline raises on every call; ``sim_fail_calls`` fails then recovers."""
    offline = SimElnAdapter({"sim_offline": True})
    with pytest.raises(ElnError):
        offline.verify()
    offline.offline = False
    assert offline.verify()

    flaky = SimElnAdapter({"sim_fail_calls": 2})
    with pytest.raises(ElnError):
        flaky.verify()
    with pytest.raises(ElnError):
        flaky.verify()
    assert flaky.verify()


def test_sim_adapter_rejects_an_unknown_entry():
    """Attaching to an entry that was never created is an ``ElnError``."""
    adapter = SimElnAdapter({})
    with pytest.raises(ElnError):
        adapter.attach_link(ElnEntryRef(entry_id="999"), "/x")


# ── Body renderers ────────────────────────────────────────────────────────────


def test_render_run_title_names_the_procedure_and_the_experiment():
    """The title carries the experiment, the procedure, and the start time."""
    assert render_run_title(MANIFEST) == "Field Sweep — 2026-01-01T10:00:00+00:00"
    assert render_run_title(MANIFEST, "Sample A").startswith("Sample A — Field Sweep")


def test_render_run_body_is_deterministic_and_complete():
    """The same manifest renders byte-identical HTML carrying every fact."""
    kwargs = {
        "experiment_id": "exp-1",
        "experiment_title": "Sample A",
        "setup": {"config_name": "sim_cryostat", "instruments": {"magnet": {"m": 1}}},
        "data_path": "/root/exp-1/data/run-0001.h5",
    }
    body = render_run_body(MANIFEST, **kwargs)
    assert body == render_run_body(MANIFEST, **kwargs)
    for expected in (
        "run-0001",
        "Field Sweep",
        "exp-1",
        "Sample A",
        "sim_cryostat",
        "field_T",
        "/root/exp-1/data/run-0001.h5",
    ):
        assert expected in body


def test_render_run_body_escapes_and_caps_values():
    """A hostile parameter cannot inject markup or a megabyte of body."""
    body = render_run_body({"run_id": "r", "params": {"note": "<script>x</script>" * 200}})
    assert "<script>" not in body
    assert "&lt;script&gt;" in body
    assert len(body) < 4000


def test_render_run_metadata_is_flat_and_json_safe():
    """The machine-readable twin of the body is flat scalars only."""
    metadata = render_run_metadata(MANIFEST, "exp-1", "/data/run-0001.h5")
    json.dumps(metadata)
    assert metadata["run_id"] == "run-0001"
    assert metadata["experiment_id"] == "exp-1"
    assert metadata["data_file"] == "/data/run-0001.h5"
    assert all(isinstance(value, str) for value in metadata.values())


# ── The outbox ────────────────────────────────────────────────────────────────


def _job(tmp_path, run_id="run-0001", **overrides):
    """Build a fully rendered publish job for ``run_id``."""
    fields = {
        "job_id": f"publish_run:{run_id}",
        "experiment_id": "exp-1",
        "run_id": run_id,
        "title": render_run_title(MANIFEST),
        "body_html": render_run_body(MANIFEST, data_path=str(tmp_path / "run.h5")),
        "tags": ["cryosoft"],
        "metadata": render_run_metadata(MANIFEST, "exp-1"),
        "data_path": str(tmp_path / "run.h5"),
    }
    fields.update(overrides)
    return OutboxJob(**fields)


def _data_file(tmp_path, size=16):
    """Write a stand-in HDF5 file and return its path."""
    path = tmp_path / "run.h5"
    path.write_bytes(b"\x89HDF\r\n\x1a\n" + b"0" * max(size - 8, 0))
    return path


def test_outbox_is_created_lazily_and_reads_empty(tmp_path):
    """Nothing touches the disk until a job is queued."""
    outbox = Outbox(tmp_path / "exp-1" / "outbox.jsonl")
    assert outbox.jobs() == []
    assert outbox.pending() == []
    assert not outbox.path.exists()


def test_outbox_enqueue_is_idempotent_by_job_id(tmp_path):
    """A duplicate job id appends nothing — the whole track's dedup point."""
    _data_file(tmp_path)
    outbox = Outbox(tmp_path / "outbox.jsonl")
    assert outbox.enqueue(_job(tmp_path)) is True
    assert outbox.enqueue(_job(tmp_path)) is False
    assert len(outbox.jobs()) == 1
    assert outbox.path.read_text(encoding="utf-8").count("\n") == 1


def test_outbox_rejects_a_job_without_an_id(tmp_path):
    """An unidentifiable job could never be deduplicated or resumed."""
    outbox = Outbox(tmp_path / "outbox.jsonl")
    with pytest.raises(ValueError):
        outbox.enqueue(OutboxJob(run_id="r"))


def test_outbox_survives_a_restart(tmp_path):
    """A queued job is still queued when a brand-new Outbox opens the file."""
    _data_file(tmp_path)
    path = tmp_path / "outbox.jsonl"
    Outbox(path).enqueue(_job(tmp_path))
    reopened = Outbox(path)
    assert [job.run_id for job in reopened.pending()] == ["run-0001"]


def test_outbox_skips_a_corrupt_line_without_stranding_the_queue(tmp_path):
    """One mangled line is skipped with a warning; the rest still drain."""
    _data_file(tmp_path)
    path = tmp_path / "outbox.jsonl"
    outbox = Outbox(path)
    outbox.enqueue(_job(tmp_path))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
        handle.write('{"kind": "publish_run"}\n')  # no job_id
    assert [job.job_id for job in outbox.jobs()] == ["publish_run:run-0001"]


def test_outbox_drain_publishes_once_and_marks_the_job_done(tmp_path):
    """A healthy drain creates the entry, attaches the data, and finishes."""
    data = _data_file(tmp_path)
    outbox = Outbox(tmp_path / "outbox.jsonl")
    outbox.enqueue(_job(tmp_path))
    adapter = SimElnAdapter({})

    result = outbox.drain(adapter)
    assert result.state == DRAIN_PUBLISHED
    assert result.run_id == "run-0001"
    assert result.entry is not None and result.entry.entry_id
    assert adapter.entries[result.entry.entry_id]["body_html"].startswith("<h3>Run</h3>")
    assert adapter.uploads[0]["path"] == str(data)
    assert outbox.pending() == []

    assert outbox.drain(adapter).state == DRAIN_IDLE
    assert adapter.calls.count("create_entry") == 1


def test_outbox_drain_is_idle_on_an_empty_queue(tmp_path):
    """Nothing queued is not an error."""
    assert Outbox(tmp_path / "outbox.jsonl").drain(SimElnAdapter({})).state == DRAIN_IDLE


def test_outbox_keeps_a_job_while_the_notebook_is_offline(tmp_path):
    """Offline leaves the job queued; a later drain publishes it exactly once."""
    _data_file(tmp_path)
    path = tmp_path / "outbox.jsonl"
    outbox = Outbox(path, retry_base_s=0.0, retry_max_s=0.0)
    outbox.enqueue(_job(tmp_path))
    adapter = SimElnAdapter({"sim_offline": True})

    result = outbox.drain(adapter)
    assert result.state == DRAIN_RETRY
    assert result.detail
    queued = outbox.get("publish_run:run-0001")
    assert queued.state == "pending" and queued.attempts == 1
    assert queued.last_error and queued.next_attempt_utc
    assert not adapter.entries

    adapter.offline = False
    assert outbox.drain(adapter).state == DRAIN_PUBLISHED
    assert len(adapter.entries) == 1
    assert outbox.drain(adapter).state == DRAIN_IDLE
    assert len(adapter.entries) == 1


def test_outbox_backoff_is_persisted_and_grows(tmp_path):
    """A failed job is not retried until its journaled backoff has elapsed."""
    from datetime import datetime, timedelta, timezone

    _data_file(tmp_path)
    outbox = Outbox(tmp_path / "outbox.jsonl", retry_base_s=60.0, retry_max_s=600.0)
    outbox.enqueue(_job(tmp_path))
    adapter = SimElnAdapter({"sim_offline": True})
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    assert outbox.drain(adapter, now=now).state == DRAIN_RETRY
    assert outbox.drain(adapter, now=now + timedelta(seconds=30)).state == DRAIN_IDLE
    assert outbox.drain(adapter, now=now + timedelta(seconds=61)).state == DRAIN_RETRY
    assert outbox.get("publish_run:run-0001").attempts == 2
    # Second failure doubles the delay: still not due 61 s later, due at 121 s.
    assert outbox.drain(adapter, now=now + timedelta(seconds=122)).state == DRAIN_IDLE
    assert outbox.drain(adapter, now=now + timedelta(seconds=183)).state == DRAIN_RETRY


def test_outbox_retry_after_a_failed_attachment_reuses_the_entry(tmp_path):
    """The partial-failure case: never a second entry for the same job."""
    _data_file(tmp_path)
    outbox = Outbox(tmp_path / "outbox.jsonl", retry_base_s=0.0, retry_max_s=0.0)
    outbox.enqueue(_job(tmp_path))
    adapter = SimElnAdapter({"sim_reject_attachments": True})

    assert outbox.drain(adapter).state == DRAIN_RETRY
    created = outbox.get("publish_run:run-0001").entry_ref()
    assert created.entry_id in adapter.entries

    adapter._reject_attachments = False
    result = outbox.drain(adapter)
    assert result.state == DRAIN_PUBLISHED
    assert result.entry == created
    assert adapter.calls.count("create_entry") == 1
    assert len(adapter.entries) == 1


def test_outbox_links_a_file_above_the_attachment_cap(tmp_path):
    """Above the cap the entry records where the data lives instead."""
    data = _data_file(tmp_path, size=4096)
    outbox = Outbox(tmp_path / "outbox.jsonl")
    outbox.enqueue(_job(tmp_path, max_attachment_bytes=64))
    adapter = SimElnAdapter({})

    assert outbox.drain(adapter).state == DRAIN_PUBLISHED
    assert not adapter.uploads
    assert adapter.links[0]["url"] == str(data)
    assert "attachment cap" in adapter.links[0]["comment"]


def test_outbox_links_a_missing_data_file(tmp_path):
    """A data file that is not there is still recorded, never silently dropped."""
    outbox = Outbox(tmp_path / "outbox.jsonl")
    outbox.enqueue(_job(tmp_path))  # no file written
    adapter = SimElnAdapter({})

    assert outbox.drain(adapter).state == DRAIN_PUBLISHED
    assert not adapter.uploads
    assert "not found" in adapter.links[0]["comment"]


def test_outbox_drain_never_raises_on_a_misbehaving_adapter(tmp_path):
    """A non-ElnError out of an adapter is contained, not propagated to the timer."""
    _data_file(tmp_path)
    outbox = Outbox(tmp_path / "outbox.jsonl", retry_base_s=0.0, retry_max_s=0.0)
    outbox.enqueue(_job(tmp_path))

    class BrokenAdapter(SimElnAdapter):
        def create_entry(self, title, template_id, body_html, tags, metadata):
            raise RuntimeError("backend client blew up")

    result = outbox.drain(BrokenAdapter({}))
    assert result.state == DRAIN_RETRY
    assert "RuntimeError" in result.detail
    assert outbox.get("publish_run:run-0001").attempts == 1
