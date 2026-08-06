"""Red-proof tests — Component E of the publication broker design
(docs/plans/2026-08-06-publication-broker-design.md): "require_test" only proves a test
FILE exists, not that it discriminates. `super_worker.red_proof` (gated on `require_test`
also being on) runs each changed test file against a fresh detached worktree at the
pristine base; a test that already passes there doesn't prove anything and discards the
candidate (stage 'no_test'). Mirrors tests/test_code_round.py's own FakeAdapter idiom in a
self-contained fake (no cross-file coupling) — no real git/tests are ever run.
"""
from factory.orchestrator import code_round


class FakeAdapter:
    """A frozen-clean, tests-green, merge-succeeding adapter with the red-proof seam
    (add_worktree_detached / run_named_test / remove_worktree) instrumented."""
    def __init__(self, *, named_test_results=None):
        self.calls = []
        self.named_test_results = dict(named_test_results or {})
        self.merge_messages = []

    def frozen_paths(self):
        return []

    def run_tests(self, repo, **k):
        self.calls.append("run_tests")
        return (True, "ok")

    def merge_branch(self, repo, branch, message=None, **k):
        self.calls.append(("merge", branch))
        self.merge_messages.append(message)
        return "MERGESHA"

    def revert_commit(self, repo, sha):
        self.calls.append(("revert", sha))
        return "REVERTSHA"

    def current_commit(self, repo):
        return "HEAD"

    def add_worktree_detached(self, repo, dest, sha):
        self.calls.append(("add_worktree_detached", repo, dest, sha))
        return dest

    def remove_worktree(self, repo, dest):
        self.calls.append(("remove_worktree", repo, dest))

    def run_named_test(self, cwd, ref, **k):
        self.calls.append(("run_named_test", cwd, ref))
        return self.named_test_results.get(ref, ("failed", "report"))


CHAMP = {"working": 0.8, "held_out": 0.7}


def _g(working=0.85, held_out=0.7):
    return {"working": working, "held_out": held_out, "held_out_measured": True,
           "divergence_alarm": False, "safety_flag": False}


def _grade(*values):
    it = iter(values)
    return lambda repo: next(it)


def _run(ad, *, changed, require_test=True, red_proof=True, base_repo="/basewt",
         base_sha="basesha1", grade_values=(_g(), _g())):
    return code_round.run_code_round(
        adapter=ad, main_repo="/main", cand_repo="/cand", branch="cand",
        changed_paths=changed, champion_scores=CHAMP, grade_fn=_grade(*grade_values),
        label="cand", require_test=require_test, red_proof=red_proof,
        base_repo=base_repo, base_sha=base_sha)


def _worktree_calls(ad):
    return [c for c in ad.calls if isinstance(c, tuple) and c[0] == "add_worktree_detached"]


def _named_test_calls(ad):
    return [c for c in ad.calls if isinstance(c, tuple) and c[0] == "run_named_test"]


# -- gating: red_proof / require_test / base_repo+base_sha -------------------------------
def test_red_proof_off_never_touches_the_worktree_seam():
    ad = FakeAdapter(named_test_results={"tests/test_x.py": ("passed", "")})
    res = _run(ad, changed=["src/x.py", "tests/test_x.py"], red_proof=False)
    assert res["action"] == "merged"
    assert _worktree_calls(ad) == []


def test_require_test_off_skips_red_proof_even_when_the_knob_is_on():
    """Design: 'runs only when require_test... resolves the test gate on' — red_proof is
    gated behind require_test, not independent of it."""
    ad = FakeAdapter(named_test_results={"tests/test_x.py": ("passed", "")})
    res = _run(ad, changed=["src/x.py", "tests/test_x.py"], require_test=False, red_proof=True)
    assert res["action"] == "merged"
    assert _worktree_calls(ad) == []


def test_missing_base_repo_or_sha_skips_silently_never_crashes():
    ad = FakeAdapter(named_test_results={"tests/test_x.py": ("passed", "")})
    res = _run(ad, changed=["src/x.py", "tests/test_x.py"], base_repo=None, base_sha=None)
    assert res["action"] == "merged"                    # no crash, no false discard
    assert _worktree_calls(ad) == []


def test_no_changed_test_file_skips_the_worktree_seam():
    """acceptance_ok already requires a test when source changed; if the diff is test-only
    with NO test files at all (docs-only / etc.) there's nothing to red-proof."""
    ad = FakeAdapter()
    res = _run(ad, changed=["docs/readme.md"])
    assert res["action"] == "merged"
    assert _worktree_calls(ad) == []


# -- the discriminating cases --------------------------------------------------------------
def test_test_that_fails_on_base_satisfies_red_proof_and_proceeds():
    ad = FakeAdapter(named_test_results={"tests/test_x.py": ("failed", "AssertionError")})
    res = _run(ad, changed=["src/x.py", "tests/test_x.py"])
    assert res["action"] == "merged"
    calls = _named_test_calls(ad)
    assert len(calls) == 1 and calls[0][2] == "tests/test_x.py"


def test_test_missing_on_base_satisfies_red_proof_and_proceeds():
    """The candidate's test file didn't exist on the pristine base at all — the strongest
    possible discrimination (nothing to trivially pass)."""
    ad = FakeAdapter(named_test_results={"tests/test_x.py": ("missing", "no such file")})
    res = _run(ad, changed=["src/x.py", "tests/test_x.py"])
    assert res["action"] == "merged"


def test_test_that_passes_on_base_discards_as_no_test():
    ad = FakeAdapter(named_test_results={"tests/test_x.py": ("passed", "1 passed")})
    res = _run(ad, changed=["src/x.py", "tests/test_x.py"])
    assert res["action"] == "discarded" and res["stage"] == "no_test"
    assert "test passes on the pristine base" in res["why"]
    assert "tests/test_x.py" in res["why"]
    assert res["tests_report"] == "1 passed"
    assert "run_tests" not in ad.calls                    # never reached the (expensive) suite gate


def test_worktree_is_always_cleaned_up_on_discard_and_on_proceed():
    ad_discard = FakeAdapter(named_test_results={"tests/test_x.py": ("passed", "")})
    _run(ad_discard, changed=["src/x.py", "tests/test_x.py"])
    assert any(c[0] == "remove_worktree" for c in ad_discard.calls if isinstance(c, tuple))

    ad_proceed = FakeAdapter(named_test_results={"tests/test_x.py": ("failed", "")})
    _run(ad_proceed, changed=["src/x.py", "tests/test_x.py"])
    assert any(c[0] == "remove_worktree" for c in ad_proceed.calls if isinstance(c, tuple))


def test_multiple_test_files_fail_fast_on_the_first_passer():
    ad = FakeAdapter(named_test_results={
        "tests/test_a.py": ("failed", ""),              # discriminates — checked, passes through
        "tests/test_b.py": ("passed", ""),               # doesn't discriminate — should stop here
        "tests/test_c.py": ("failed", ""),               # never reached
    })
    res = _run(ad, changed=["src/x.py", "tests/test_a.py", "tests/test_b.py", "tests/test_c.py"])
    assert res["action"] == "discarded" and res["stage"] == "no_test"
    assert "tests/test_b.py" in res["why"]
    checked_refs = [c[2] for c in _named_test_calls(ad)]
    assert checked_refs == ["tests/test_a.py", "tests/test_b.py"]   # c.py never checked


def test_only_test_classified_changed_paths_are_checked():
    ad = FakeAdapter(named_test_results={"tests/test_x.py": ("failed", "")})
    res = _run(ad, changed=["src/x.py", "src/y.py", "tests/test_x.py"])
    assert res["action"] == "merged"
    checked_refs = [c[2] for c in _named_test_calls(ad)]
    assert checked_refs == ["tests/test_x.py"]           # src/x.py, src/y.py never sent through


def test_worktree_built_at_the_recorded_base_sha_in_base_repo():
    ad = FakeAdapter(named_test_results={"tests/test_x.py": ("failed", "")})
    _run(ad, changed=["src/x.py", "tests/test_x.py"], base_repo="/dev/clone", base_sha="deadbeef")
    (repo, dest, sha), = [c[1:] for c in _worktree_calls(ad)]
    assert repo == "/dev/clone" and sha == "deadbeef"


def test_red_proof_runs_before_the_full_test_suite():
    """Fail fast, one file at a time, BEFORE the (expensive) full suite gate."""
    ad = FakeAdapter(named_test_results={"tests/test_x.py": ("passed", "")})
    _run(ad, changed=["src/x.py", "tests/test_x.py"])
    assert "run_tests" not in ad.calls
