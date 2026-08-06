"""reporting/envelope.py — Component A of the publication broker
(docs/plans/2026-08-06-publication-broker-design.md). Pure I/O tests: no store, no
subprocess, no real git — the broker's own verification logic is exercised end-to-end
(against real hermetic git repos) in tests/test_broker.py.
"""
import os
from datetime import datetime, timedelta, timezone

from factory.common import paths
from factory.reporting import envelope


def _env(**over):
    base = dict(action="graduate", repo_slug="o/r", base_branch="base",
               base_sha="b0" * 20, tip_sha="t1" * 20, range_="b0..t1", n_commits=2,
               approval_id=7, policy_hash="deadbeef")
    base.update(over)
    return envelope.build_envelope(**base)


# -- build_envelope -----------------------------------------------------------------------
def test_build_envelope_shape_and_defaults():
    env = _env()
    assert env["schema_version"] == envelope.SCHEMA_VERSION
    assert env["action"] == "graduate"
    assert env["repo_slug"] == "o/r"
    assert env["base_branch"] == "base"
    assert env["n_commits"] == 2
    assert env["issue_actions"] == []
    assert env["approval_id"] == 7
    assert env["policy_hash"] == "deadbeef"
    assert env["nonce"]                                 # a fresh uuid4 hex
    assert env["created_at"] < env["expires_at"]         # ISO strings sort chronologically


def test_build_envelope_two_calls_mint_different_nonces():
    assert _env()["nonce"] != _env()["nonce"]


def test_build_envelope_honors_an_injected_nonce_and_clock():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    env = _env(nonce="fixed-nonce", now=now, ttl_hours=1)
    assert env["nonce"] == "fixed-nonce"
    assert env["created_at"] == "2026-01-01T00:00:00.000000Z"
    assert env["expires_at"] == "2026-01-01T01:00:00.000000Z"


def test_build_envelope_carries_issue_actions_through():
    acts = [{"op": "close", "number": 12, "body": "closes #12"}]
    env = _env(issue_actions=acts)
    assert env["issue_actions"] == acts


# -- content_hash / verify_hash ------------------------------------------------------------
def test_content_hash_is_stable_for_equal_content():
    a, b = _env(nonce="n1"), _env(nonce="n1")
    # every OTHER field differs only in created_at/expires_at unless we pin the clock too
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    a = _env(nonce="n1", now=now)
    b = _env(nonce="n1", now=now)
    assert envelope.content_hash(a) == envelope.content_hash(b)


def test_content_hash_changes_when_any_field_changes():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    a = _env(nonce="n1", now=now)
    b = _env(nonce="n1", now=now, n_commits=99)
    assert envelope.content_hash(a) != envelope.content_hash(b)


def test_verify_hash_true_for_an_untouched_sidecar(tmp_path):
    env = _env()
    _, hash_path = envelope.write_envelope(env, str(tmp_path))
    assert envelope.verify_hash(env, hash_path) is True


def test_verify_hash_false_when_the_envelope_was_tampered(tmp_path):
    env = _env()
    json_path, hash_path = envelope.write_envelope(env, str(tmp_path))
    tampered = dict(env, n_commits=999)
    assert envelope.verify_hash(tampered, hash_path) is False


def test_verify_hash_false_when_the_sidecar_is_missing(tmp_path):
    env = _env()
    assert envelope.verify_hash(env, str(tmp_path / "nope.sha256")) is False


# -- write_envelope / read_envelope ---------------------------------------------------------
def test_write_then_read_roundtrips(tmp_path):
    env = _env()
    json_path, hash_path = envelope.write_envelope(env, str(tmp_path))
    assert json_path.endswith(f"{env['nonce']}.json")
    assert hash_path == f"{json_path}.sha256"
    got = envelope.read_envelope(json_path)
    assert got == env


def test_read_envelope_returns_none_for_missing_file(tmp_path):
    assert envelope.read_envelope(str(tmp_path / "missing.json")) is None


def test_read_envelope_returns_none_for_malformed_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    assert envelope.read_envelope(str(p)) is None


def test_write_envelope_creates_the_outbox_dir(tmp_path):
    outbox = tmp_path / "nested" / "outbox"
    envelope.write_envelope(_env(), str(outbox))
    assert outbox.is_dir()


# -- is_expired -------------------------------------------------------------------------
def test_is_expired_false_within_ttl():
    env = _env(ttl_hours=24)
    assert envelope.is_expired(env) is False


def test_is_expired_true_past_ttl():
    past = datetime.now(timezone.utc) - timedelta(hours=48)
    env = _env(now=past, ttl_hours=1)
    assert envelope.is_expired(env) is True


def test_is_expired_true_at_the_exact_boundary():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    env = _env(now=now, ttl_hours=1)
    exactly_then = now + timedelta(hours=1)
    assert envelope.is_expired(env, now=exactly_then) is True


def test_is_expired_fails_closed_on_missing_expiry():
    env = _env()
    del env["expires_at"]
    assert envelope.is_expired(env) is True


def test_is_expired_fails_closed_on_malformed_expiry():
    env = _env(); env["expires_at"] = "not-a-date"
    assert envelope.is_expired(env) is True


# -- list_outbox --------------------------------------------------------------------------
def test_list_outbox_empty_for_missing_dir(tmp_path):
    assert envelope.list_outbox(str(tmp_path / "nope")) == []


def test_list_outbox_lists_nonces_oldest_first(tmp_path):
    import time
    e1 = _env(nonce="n1")
    envelope.write_envelope(e1, str(tmp_path))
    time.sleep(0.02)
    e2 = _env(nonce="n2")
    envelope.write_envelope(e2, str(tmp_path))
    assert envelope.list_outbox(str(tmp_path)) == ["n1", "n2"]


def test_list_outbox_ignores_sha256_sidecars_and_tmp_files(tmp_path):
    envelope.write_envelope(_env(nonce="n1"), str(tmp_path))
    (tmp_path / "n1.json.tmp-999").write_text("partial", encoding="utf-8")
    assert envelope.list_outbox(str(tmp_path)) == ["n1"]


# -- receipts -------------------------------------------------------------------------------
def test_write_read_receipt_roundtrip(tmp_path):
    path = envelope.write_receipt(nonce="n1", status="pushed", receipts_dir=str(tmp_path),
                                  receipt_sha="abc123", detail="ok", policy_hash="ph")
    assert path.endswith("n1.receipt.json")
    r = envelope.read_receipt(str(tmp_path), "n1")
    assert r["nonce"] == "n1" and r["status"] == "pushed" and r["receipt_sha"] == "abc123"
    assert r["detail"] == "ok" and r["policy_hash"] == "ph" and r["executed_at"]


def test_read_receipt_none_when_absent(tmp_path):
    assert envelope.read_receipt(str(tmp_path), "nope") is None


def test_has_receipt(tmp_path):
    assert envelope.has_receipt(str(tmp_path), "n1") is False
    envelope.write_receipt(nonce="n1", status="rejected", receipts_dir=str(tmp_path))
    assert envelope.has_receipt(str(tmp_path), "n1") is True


def test_list_receipts_only_the_live_top_level(tmp_path):
    envelope.write_receipt(nonce="n1", status="pushed", receipts_dir=str(tmp_path))
    envelope.write_receipt(nonce="n2", status="rejected", receipts_dir=str(tmp_path))
    done = tmp_path / "done"
    envelope.archive_receipt(str(tmp_path), "n1", str(done))
    assert envelope.list_receipts(str(tmp_path)) == ["n2"]


def test_list_receipts_empty_for_missing_dir(tmp_path):
    assert envelope.list_receipts(str(tmp_path / "nope")) == []


def test_archive_receipt_moves_it_and_is_idempotent_on_repeat(tmp_path):
    envelope.write_receipt(nonce="n1", status="pushed", receipts_dir=str(tmp_path))
    done = tmp_path / "done"
    moved = envelope.archive_receipt(str(tmp_path), "n1", str(done))
    assert moved == str(done / "n1.receipt.json")
    assert not (tmp_path / "n1.receipt.json").exists()
    assert (done / "n1.receipt.json").exists()
    # a second archive of the SAME (now-consumed) nonce is a harmless no-op, not an error
    assert envelope.archive_receipt(str(tmp_path), "n1", str(done)) is None


# -- policy_hash ----------------------------------------------------------------------------
def test_policy_hash_matches_a_direct_sha256_of_config_yaml():
    import hashlib
    with open(paths.CONFIG_YAML, "rb") as fh:
        expected = hashlib.sha256(fh.read()).hexdigest()
    assert envelope.policy_hash() == expected


def test_policy_hash_changes_with_content(tmp_path):
    p = tmp_path / "a.yaml"
    p.write_text("a: 1\n", encoding="utf-8")
    h1 = envelope.policy_hash(str(p))
    p.write_text("a: 2\n", encoding="utf-8")
    h2 = envelope.policy_hash(str(p))
    assert h1 != h2


def test_policy_hash_empty_string_when_unreadable(tmp_path):
    assert envelope.policy_hash(str(tmp_path / "missing.yaml")) == ""


# -- paths.py broker spool helpers -----------------------------------------------------------
def test_broker_paths_default_under_factory_root_state_broker():
    root = paths.broker_spool_root()
    assert root.endswith(os.path.join("state", "broker"))
    assert paths.broker_outbox_dir().endswith(os.path.join("state", "broker", "outbox"))
    assert paths.broker_receipts_dir().endswith(os.path.join("state", "broker", "receipts"))
    assert paths.broker_receipts_done_dir().endswith(
        os.path.join("state", "broker", "receipts", "done"))
    assert paths.broker_bare_repo().endswith(
        os.path.join("state", "broker", "clive-publish.git"))


def test_broker_paths_honor_an_explicit_root_override(tmp_path):
    root = str(tmp_path / "spool")
    assert paths.broker_outbox_dir(root) == str(tmp_path / "spool" / "outbox")
    assert paths.broker_receipts_dir(root) == str(tmp_path / "spool" / "receipts")
    assert paths.broker_bare_repo(root) == str(tmp_path / "spool" / "clive-publish.git")


def test_broker_paths_honor_env_var_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTORY_BROKER_SPOOL", str(tmp_path / "envspool"))
    assert paths.broker_spool_root() == str(tmp_path / "envspool")
    monkeypatch.setenv("FACTORY_BROKER_BARE", str(tmp_path / "elsewhere.git"))
    assert paths.broker_bare_repo() == str(tmp_path / "elsewhere.git")


def test_broker_allowlist_path_default_and_override(tmp_path, monkeypatch):
    monkeypatch.delenv("FACTORY_BROKER_ALLOWLIST", raising=False)
    assert paths.broker_allowlist_path().endswith(".factory-broker.yaml")
    assert paths.broker_allowlist_path(str(tmp_path / "x.yaml")) == str(tmp_path / "x.yaml")
    monkeypatch.setenv("FACTORY_BROKER_ALLOWLIST", str(tmp_path / "env.yaml"))
    assert paths.broker_allowlist_path() == str(tmp_path / "env.yaml")
