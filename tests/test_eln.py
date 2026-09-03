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
