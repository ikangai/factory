"""Backfill: the spec gate (common/specs.py) — pure functions that bound what a
candidate may change. The security-relevant invariant is that a candidate may
NOT mutate the frozen block (permission gates / scope limits / destructive-action
policy) and may change at most one open key ("one bounded change")."""
from factory.common import specs


def _champion():
    return {
        "meta": {"version": 1, "parent": "root"},
        "open": {"system_prompt": "base", "skills": [], "recovery_policy": "retry"},
        "frozen": {"permission_gates": ["confirm-destructive"], "scope_limits": {"max": 1}},
    }


def _candidate(champ, **open_overrides):
    cand = {"meta": {"version": 2, "parent": "champion"},
            "open": {**champ["open"], **open_overrides},
            "frozen": dict(champ["frozen"])}
    return cand


# ── hashing ────────────────────────────────────────────────────────────────
def test_compute_hash_is_deterministic_and_key_order_independent():
    a = specs.compute_hash({"x": 1, "y": 2}, {"z": 3})
    b = specs.compute_hash({"y": 2, "x": 1}, {"z": 3})  # different insertion order
    assert a == b
    assert a != specs.compute_hash({"x": 1, "y": 99}, {"z": 3})


def test_verify_hash_detects_tampering():
    spec = _champion()
    spec["meta"]["hash"] = specs.compute_hash(spec["open"], spec["frozen"])
    assert specs.verify_hash(spec) is True
    spec["open"]["system_prompt"] = "tampered"   # change content, leave the hash
    assert specs.verify_hash(spec) is False


# ── open_diff ──────────────────────────────────────────────────────────────
def test_open_diff_reports_changed_keys_with_from_to():
    diff = specs.open_diff({"a": 1, "b": 2}, {"a": 1, "b": 9, "c": 3})
    assert set(diff) == {"b", "c"}
    assert diff["b"] == {"from": 2, "to": 9}
    assert diff["c"] == {"from": None, "to": 3}


# ── validate_candidate ──────────────────────────────────────────────────────
def test_valid_one_key_change_passes():
    champ = _champion()
    cand = _candidate(champ, system_prompt="base + a tweak")
    res = specs.validate_candidate(cand, champ)
    assert res.ok, res.errors
    assert res.changed_keys == ["system_prompt"]


def test_missing_parent_is_rejected():
    champ = _champion()
    cand = _candidate(champ, system_prompt="x")
    cand["meta"]["parent"] = ""
    res = specs.validate_candidate(cand, champ)
    assert not res.ok
    assert any("parent" in e for e in res.errors)


def test_mutating_the_frozen_block_is_rejected():
    champ = _champion()
    cand = _candidate(champ, system_prompt="x")
    cand["frozen"]["permission_gates"] = []        # weaken a safety gate
    res = specs.validate_candidate(cand, champ)
    assert not res.ok
    assert any("frozen" in e for e in res.errors)


def test_identical_open_is_rejected_as_no_change():
    champ = _champion()
    cand = _candidate(champ)                        # no open override → identical
    res = specs.validate_candidate(cand, champ)
    assert not res.ok
    assert any("identical" in e for e in res.errors)


def test_too_many_changed_keys_is_rejected():
    champ = _champion()
    cand = _candidate(champ, system_prompt="x", recovery_policy="abort")  # 2 keys
    res = specs.validate_candidate(cand, champ)
    assert not res.ok
    assert any("max allowed is 1" in e for e in res.errors)


def test_max_changed_open_keys_can_be_raised():
    champ = _champion()
    cand = _candidate(champ, system_prompt="x", recovery_policy="abort")
    res = specs.validate_candidate(cand, champ, max_changed_open_keys=2)
    assert res.ok, res.errors
    assert set(res.changed_keys) == {"system_prompt", "recovery_policy"}


def test_self_inconsistent_hash_is_rejected():
    champ = _champion()
    cand = _candidate(champ, system_prompt="x")
    cand["meta"]["hash"] = "deadbeef"              # present but wrong
    res = specs.validate_candidate(cand, champ)
    assert not res.ok
    assert any("hash" in e for e in res.errors)
