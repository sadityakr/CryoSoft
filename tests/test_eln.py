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
from cryosoft.session.eln.elabftw import (
    ElabFtwAdapter,
    HttpResponse,
    UrllibTransport,
)
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


# ── The eLabFTW backend (against a fake transport — never a live server) ──────


class FakeTransport:
    """Canned HTTP responses plus a full record of what was requested."""

    def __init__(self, responses=None):
        """Queue one response per expected call, keyed by ``"METHOD /path"``."""
        self.responses = dict(responses or {})
        self.calls = []

    def request(self, method, url, headers, body, timeout_s):
        """Record the call and answer from the canned table."""
        path = url.split("/api/v2", 1)[-1]
        self.calls.append(
            {
                "method": method,
                "url": url,
                "path": path,
                "headers": dict(headers),
                "body": body,
                "timeout_s": timeout_s,
            }
        )
        key = f"{method} {path}"
        if key not in self.responses:
            return HttpResponse(status=404, body=b'{"description":"not found"}')
        canned = self.responses[key]
        return canned.pop(0) if isinstance(canned, list) else canned


def _elab(responses=None, **settings):
    """Build an eLabFTW adapter over a fake transport."""
    base = {
        "enabled": True,
        "base_url": "https://elab.example.org",
        "api_key": "top-secret-key",
    }
    base.update(settings)
    transport = FakeTransport(responses)
    return ElabFtwAdapter(base, transport=transport), transport


def test_elabftw_verify_names_the_account_and_sends_the_token():
    """The health check authenticates and reports who the key belongs to."""
    adapter, transport = _elab(
        {"GET /users/me": HttpResponse(200, {}, b'{"fullname":"A Sen","team_name":"Cryo"}')}
    )
    assert adapter.verify() == "A Sen (Cryo)"
    call = transport.calls[0]
    assert call["url"] == "https://elab.example.org/api/v2/users/me"
    assert call["headers"]["Authorization"] == "top-secret-key"
    assert call["timeout_s"] == 15.0


def test_elabftw_maps_a_rejected_key_to_an_eln_error_without_leaking_it():
    """A 401 becomes an ``ElnError`` naming the status, never the key."""
    adapter, _ = _elab({"GET /users/me": HttpResponse(401, {}, b'{"description":"bad key"}')})
    with pytest.raises(ElnError) as excinfo:
        adapter.verify()
    assert "401" in str(excinfo.value)
    assert "top-secret-key" not in str(excinfo.value)


def test_elabftw_refuses_to_call_without_a_url_or_a_key():
    """Half-configured settings fail as an ``ElnError``, not a stray exception."""
    no_url, _ = _elab(base_url="")
    with pytest.raises(ElnError):
        no_url.verify()
    no_key, _ = _elab(api_key="")
    with pytest.raises(ElnError):
        no_key.verify()


def test_elabftw_lists_templates():
    """Templates come back as ``ElnTemplate``s, junk entries ignored."""
    adapter, _ = _elab(
        {
            "GET /experiments_templates": HttpResponse(
                200, {}, b'[{"id":7,"title":"Cryostat run"},"junk"]'
            )
        }
    )
    templates = adapter.list_templates()
    assert [(t.template_id, t.name) for t in templates] == [("7", "Cryostat run")]


def test_elabftw_creates_an_entry_from_a_template_then_fills_it_in():
    """Create + patch: the id comes from the Location header, the URL is human-facing."""
    adapter, transport = _elab(
        {
            "POST /experiments": HttpResponse(
                201, {"location": "https://elab.example.org/api/v2/experiments/42"}, b""
            ),
            "PATCH /experiments/42": HttpResponse(200, {}, b"{}"),
        },
        template_id="7",
    )
    ref = adapter.create_entry("Run 1", None, "<p>body</p>", ["cryosoft"], {"run_id": "r1"})
    assert ref.backend == "elabftw"
    assert ref.entry_id == "42"
    assert ref.template_id == "7"
    assert ref.url == "https://elab.example.org/experiments.php?mode=view&id=42"

    create, patch = transport.calls
    assert json.loads(create["body"]) == {"template": "7"}
    payload = json.loads(patch["body"])
    assert payload["title"] == "Run 1"
    assert payload["body"] == "<p>body</p>"
    assert payload["tags"] == ["cryosoft"]
    assert json.loads(payload["metadata"]) == {"run_id": "r1"}


def test_elabftw_falls_back_to_the_body_for_the_new_entry_id():
    """A deployment that echoes the id in the body instead of a header still works."""
    adapter, _ = _elab(
        {
            "POST /experiments": HttpResponse(201, {}, b'{"id":9}'),
            "PATCH /experiments/9": HttpResponse(200, {}, b"{}"),
        }
    )
    assert adapter.create_entry("T", None, "", [], {}).entry_id == "9"


def test_elabftw_create_without_an_id_is_an_eln_error():
    """An entry CryoSoft cannot address again is a failure, not a silent success."""
    adapter, _ = _elab({"POST /experiments": HttpResponse(201, {}, b"")})
    with pytest.raises(ElnError):
        adapter.create_entry("T", None, "", [], {})


def test_elabftw_updates_an_entry():
    """``update_entry`` rewrites body and metadata in one PATCH."""
    adapter, transport = _elab({"PATCH /experiments/42": HttpResponse(200, {}, b"{}")})
    adapter.update_entry(ElnEntryRef(entry_id="42"), "<p>new</p>", {"k": "v"})
    payload = json.loads(transport.calls[0]["body"])
    assert payload["body"] == "<p>new</p>"
    assert json.loads(payload["metadata"]) == {"k": "v"}


def test_elabftw_uploads_a_file_as_multipart(tmp_path):
    """The upload is a multipart POST carrying the filename and the bytes."""
    data = tmp_path / "run-0001.h5"
    data.write_bytes(b"\x89HDF\r\n\x1a\nPAYLOAD")
    adapter, transport = _elab(
        {"POST /experiments/42/uploads": HttpResponse(201, {}, b"{}")}
    )
    adapter.attach_file(ElnEntryRef(entry_id="42"), data, "Run data")

    call = transport.calls[0]
    assert call["headers"]["Content-Type"].startswith("multipart/form-data; boundary=")
    assert b'name="file"; filename="run-0001.h5"' in call["body"]
    assert b"PAYLOAD" in call["body"]
    assert b'name="comment"' in call["body"]


def test_elabftw_upload_of_a_missing_file_is_an_eln_error(tmp_path):
    """An unreadable file fails before anything is sent."""
    adapter, transport = _elab()
    with pytest.raises(ElnError):
        adapter.attach_file(ElnEntryRef(entry_id="42"), tmp_path / "gone.h5")
    assert transport.calls == []


def test_elabftw_attaches_a_link_by_appending_to_the_body():
    """With no link endpoint, the path is appended to the entry body, escaped."""
    adapter, transport = _elab(
        {
            "GET /experiments/42": HttpResponse(200, {}, b'{"body":"<p>existing</p>"}'),
            "PATCH /experiments/42": HttpResponse(200, {}, b"{}"),
        }
    )
    adapter.attach_link(ElnEntryRef(entry_id="42"), "/data/<run>.h5", "Run data")
    body = json.loads(transport.calls[1]["body"])["body"]
    assert body.startswith("<p>existing</p>")
    assert "&lt;run&gt;" in body and "<run>" not in body
    assert "Run data" in body


def test_elabftw_reports_a_non_json_body_as_an_eln_error():
    """An HTML error page from a proxy is a backend failure, not a crash."""
    adapter, _ = _elab({"GET /users/me": HttpResponse(200, {}, b"<html>hi</html>")})
    with pytest.raises(ElnError):
        adapter.verify()


def test_elabftw_publishes_end_to_end_through_the_outbox(tmp_path):
    """The outbox drives the real backend exactly as it drives the sim twin."""
    data = _data_file(tmp_path)
    adapter, transport = _elab(
        {
            "POST /experiments": HttpResponse(
                201, {"location": "/api/v2/experiments/42"}, b""
            ),
            "PATCH /experiments/42": HttpResponse(200, {}, b"{}"),
            "POST /experiments/42/uploads": HttpResponse(201, {}, b"{}"),
        }
    )
    outbox = Outbox(tmp_path / "outbox.jsonl")
    outbox.enqueue(_job(tmp_path))

    result = outbox.drain(adapter)
    assert result.state == DRAIN_PUBLISHED
    assert result.entry.url.endswith("id=42")
    assert [call["method"] for call in transport.calls] == ["POST", "PATCH", "POST"]
    assert data.name.encode() in transport.calls[2]["body"]


def test_urllib_transport_refuses_to_verify_when_told_to(caplog):
    """Disabling TLS verification is possible, deliberate, and always logged."""
    import logging

    with caplog.at_level(logging.WARNING):
        UrllibTransport(verify_tls=False)
    assert any("verification is DISABLED" in record.message for record in caplog.records)
    UrllibTransport()  # the default verifies, silently


# ── The publisher (end to end, over a real ExperimentManager) ─────────────────


@pytest.fixture
def published_setup(tmp_path, qtbot):
    """A real ExperimentManager with an open experiment, plus a sim notebook.

    Yields ``(manager, publisher, adapter, run_manifest)``. The manifest is
    the shape the Orchestrator emits, and its data file really exists inside
    the experiment folder, so the whole path — relativize, resolve, attach —
    is exercised rather than stubbed.
    """
    from cryosoft.core.orchestrator import Orchestrator
    from cryosoft.core.station import build_station
    from cryosoft.session.eln.publisher import ElnPublisher
    from cryosoft.session.manager import ExperimentManager
    from cryosoft.session.models import User
    from cryosoft.session.store import ExperimentStore, UserRoster

    store = ExperimentStore(tmp_path / "experiments")
    roster = UserRoster(tmp_path / "users.json")
    roster.add(User(user_id="jdoe", name="J. Doe"))
    orchestrator = Orchestrator(build_station("cryosoft/configs/sim_cryostat"), tick_interval_ms=10)
    manager = ExperimentManager(
        store=store, roster=roster, orchestrator=orchestrator, config_name="sim_cryostat"
    )
    experiment = manager.start_experiment("Sample A", "jdoe", {"sample_name": "A3"})

    data_file = store.data_dir(experiment.experiment_id) / "run-0001.h5"
    data_file.parent.mkdir(parents=True, exist_ok=True)
    data_file.write_bytes(b"\x89HDF\r\n\x1a\n")

    started = {
        "run_id": "run-0001",
        "procedure": "Field Sweep",
        "kind": "run",
        "params": {"field_T": 1.5},
        "data_file": str(data_file),
        "started_utc": "2026-01-01T10:00:00+00:00",
    }
    orchestrator.run_started.emit(started)
    finished = dict(started, finished_utc="2026-01-01T11:00:00+00:00", status="done", reason="")

    settings = ElnSettings(
        enabled=True,
        backend="sim_eln",
        base_url="https://sim.example",
        api_key="k",
        tags=("cryosoft", "sim"),
        # No backoff: these tests step the queue by hand, and the backoff
        # itself is exercised against the Outbox directly.
        retry_base_s=0.0,
        retry_max_s=0.0,
    )
    adapter = SimElnAdapter({})
    publisher = ElnPublisher(manager, settings, adapter=adapter)
    orchestrator.run_finished.connect(publisher.on_run_finished)
    yield manager, publisher, adapter, finished
    publisher.stop()


def test_run_finished_publishes_one_entry_with_body_and_data(published_setup):
    """The exit criterion: one RunFinished, one entry, one attachment, one link."""
    manager, publisher, adapter, manifest = published_setup
    experiment = manager.current_experiment()

    manager._orchestrator.run_finished.emit(manifest)
    assert publisher.pending_count() == 1
    assert not adapter.entries, "queuing must touch no network"

    result = publisher.drain_once()
    assert result.state == DRAIN_PUBLISHED

    (entry,) = adapter.entries.values()
    assert entry["title"].startswith("Sample A — Field Sweep")
    assert "run-0001" in entry["body_html"] and "sim_cryostat" in entry["body_html"]
    assert entry["tags"] == ["cryosoft", "sim"]
    assert entry["metadata"]["run_id"] == "run-0001"
    assert adapter.uploads[0]["path"].endswith("run-0001.h5")

    run = manager.current_experiment().find_run("run-0001")
    assert run.published is True
    assert run.eln_link is not None
    assert run.eln_link.backend == "sim_eln"
    assert run.eln_link.entry_id and run.eln_link.url

    reloaded = manager.store.load(experiment.experiment_id).find_run("run-0001")
    assert reloaded.eln_link == run.eln_link, "the link is persisted, not just in memory"


def test_offline_leaves_the_job_queued_and_a_later_drain_publishes_once(published_setup):
    """The offline-first exit criterion, end to end."""
    manager, publisher, adapter, manifest = published_setup
    adapter.offline = True
    manager._orchestrator.run_finished.emit(manifest)

    assert publisher.drain_once().state == DRAIN_RETRY
    assert not adapter.entries
    assert publisher.pending_count() == 1
    assert manager.current_experiment().find_run("run-0001").eln_link is None

    adapter.offline = False
    assert publisher.drain_once().state == DRAIN_PUBLISHED
    assert len(adapter.entries) == 1
    assert publisher.drain_once().state == DRAIN_IDLE
    assert len(adapter.entries) == 1
    assert manager.current_experiment().find_run("run-0001").eln_link is not None


def test_a_duplicate_run_finished_publishes_exactly_once(published_setup):
    """Two identical RunFinished events are one job and one entry."""
    manager, publisher, adapter, manifest = published_setup
    manager._orchestrator.run_finished.emit(manifest)
    manager._orchestrator.run_finished.emit(manifest)
    assert publisher.pending_count() == 1

    assert publisher.drain_once().state == DRAIN_PUBLISHED
    assert publisher.drain_once().state == DRAIN_IDLE
    assert len(adapter.entries) == 1
    assert adapter.calls.count("create_entry") == 1


def test_the_publisher_accepts_a_run_finished_contract_event(published_setup):
    """``RunFinished`` and the Orchestrator's manifest dict are both accepted."""
    from cryosoft.core.events import RunFinished

    manager, publisher, adapter, manifest = published_setup
    event = RunFinished(run_id="run-0001", status="done", manifest=manifest)
    assert publisher.on_run_finished(event) == "publish_run:run-0001"
    assert publisher.drain_once().state == DRAIN_PUBLISHED
    assert len(adapter.entries) == 1


def test_nothing_is_published_without_an_open_experiment(published_setup):
    """No silent uploads of ad-hoc runs — an entry needs a record to belong to."""
    manager, publisher, adapter, manifest = published_setup
    manager.close_experiment()
    assert publisher.on_run_finished(manifest) == ""
    assert publisher.drain_once().state == DRAIN_IDLE
    assert not adapter.entries


def test_auto_publish_off_leaves_manual_export_as_the_only_trigger(published_setup):
    """With auto-publish off a finished run waits for an explicit export."""
    manager, publisher, adapter, manifest = published_setup
    publisher._settings = ElnSettings(
        **{**publisher.settings.to_dict(include_secret=True), "auto_publish": False}
    )
    manager._orchestrator.run_finished.emit(manifest)
    assert publisher.pending_count() == 0

    assert publisher.export_run("run-0001") == "publish_run:run-0001"
    assert publisher.drain_once().state == DRAIN_PUBLISHED
    (entry,) = adapter.entries.values()
    assert "run-0001" in entry["body_html"]


def test_export_of_an_unknown_run_queues_nothing(published_setup):
    """A manual export names a recorded run or does nothing."""
    _, publisher, adapter, _ = published_setup
    assert publisher.export_run("no-such-run") == ""
    assert publisher.pending_count() == 0


def test_disabled_settings_publish_nothing_at_all(published_setup):
    """The default (no settings file) means nothing ever leaves the machine."""
    manager, publisher, adapter, manifest = published_setup
    publisher._settings = ElnSettings()
    assert publisher.on_run_finished(manifest) == ""
    assert publisher.export_run("run-0001") == ""
    assert publisher.drain_once().state == DRAIN_IDLE
    assert not adapter.entries


def test_a_queued_job_survives_a_restart(published_setup, qtbot):
    """A publisher built afterwards adopts the outbox left on disk and drains it."""
    from cryosoft.session.eln.publisher import ElnPublisher

    manager, publisher, adapter, manifest = published_setup
    adapter.offline = True
    manager._orchestrator.run_finished.emit(manifest)
    publisher.drain_once()
    publisher.stop()

    fresh_adapter = SimElnAdapter({})
    restarted = ElnPublisher(manager, publisher.settings, adapter=fresh_adapter)
    assert restarted.pending_count() == 1
    assert restarted.drain_once().state == DRAIN_PUBLISHED
    assert len(fresh_adapter.entries) == 1
    restarted.stop()


def test_publish_state_changes_are_announced(published_setup, qtbot):
    """The status chip's signal follows queued → published."""
    manager, publisher, adapter, manifest = published_setup
    seen = []
    publisher.publish_state_changed.connect(seen.append)

    manager._orchestrator.run_finished.emit(manifest)
    publisher.drain_once()

    assert [item["state"] for item in seen] == ["pending", "synced"]
    assert seen[0]["pending"] == 1 and seen[-1]["pending"] == 0


def test_run_published_signal_carries_the_link(published_setup, qtbot):
    """A confirmed entry is announced once, with its link."""
    manager, publisher, adapter, manifest = published_setup
    seen = []
    publisher.run_published.connect(seen.append)

    manager._orchestrator.run_finished.emit(manifest)
    publisher.drain_once()

    assert len(seen) == 1
    assert seen[0]["run_id"] == "run-0001"
    assert seen[0]["eln_link"]["backend"] == "sim_eln"


def test_backends_are_discovered_not_listed():
    """A new backend module is selectable the moment its file exists."""
    from cryosoft.session.eln.publisher import discover_backends

    backends = discover_backends()
    assert backends["elabftw"] is ElabFtwAdapter
    assert backends["sim_eln"] is SimElnAdapter


def test_an_unknown_backend_disables_publishing_rather_than_raising(published_setup):
    """A typo in the settings file switches the track off, loudly, not fatally."""
    manager, publisher, _, manifest = published_setup
    publisher._adapter = None
    publisher._settings = ElnSettings(
        **{**publisher.settings.to_dict(include_secret=True), "backend": "not_a_backend"}
    )
    manager._orchestrator.run_finished.emit(manifest)
    assert publisher.drain_once().state == DRAIN_IDLE
    assert publisher.pending_count() == 1, "the job stays queued for a fixed settings file"


def test_a_link_recorded_after_the_experiment_closed_still_lands(published_setup):
    """An outbox job that drains a week later still stamps its own experiment."""
    manager, publisher, adapter, manifest = published_setup
    experiment_id = manager.current_experiment().experiment_id
    adapter.offline = True
    manager._orchestrator.run_finished.emit(manifest)
    publisher.drain_once()

    manager.close_experiment()
    adapter.offline = False
    assert publisher.drain_once().state == DRAIN_PUBLISHED

    stored = manager.store.load(experiment_id).find_run("run-0001")
    assert stored.eln_link is not None and stored.published is True
