"""Red-proof tests — Component E of the publication broker design
(docs/plans/2026-08-06-publication-broker-design.md), NODE-level targeting (F6, round-2
integration fix): "require_test" only proves a test FILE exists, not that it
discriminates. The ORIGINAL (file-level) implementation ran an entire changed test file
against the pristine base — which meant the NORMAL way to ship a discriminating test
(add a case to an existing tests/test_x.py, or fix an existing assertion in place) was
the ONE shape that got wrongly discarded: any OTHER, unrelated, already-passing test in
that same file made the file-level pytest run report a trivial 'passed', and deleting an
obsolete test file was discarded outright (nothing to "discriminate" about a deletion).

This file uses REAL files + REAL pytest subprocess runs (never a canned pass/fail) — the
AST-based node diffing this fix depends on needs real file content to compare, and a
canned-status FakeAdapter (the ORIGINAL test style here) is exactly why F6 went
undetected: it could never exercise "same node name, different body" or "node absent
from base" for real. `add_worktree_detached` populates a REAL directory (the caller
gives it a real mkdtemp'd path) with the base's file content; `cand_repo` is a real
tmp_path directory the test itself writes the candidate's content into.
"""
import subprocess
import sys
import textwrap

from factory.orchestrator import code_round


def _write(root, rel, content):
    import os
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(textwrap.dedent(content))


class RealPytestAdapter:
    """Frozen-clean, tests-green, merge-succeeding — everything EXCEPT the red-proof seam
    is canned; `add_worktree_detached`/`run_named_test` are real (real files, real
    pytest subprocess), so node-level AST diffing is exercised for real."""
    def __init__(self, *, base_files=None):
        self.calls = []
        self.merge_messages = []
        self.base_files = dict(base_files or {})

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
        for rel, content in self.base_files.items():
            _write(dest, rel, content)
        return dest

    def remove_worktree(self, repo, dest):
        self.calls.append(("remove_worktree", repo, dest))

    def run_named_test(self, cwd, ref, **k):
        self.calls.append(("run_named_test", cwd, ref))
        p = subprocess.run([sys.executable, "-m", "pytest", ref, "-q"],
                           cwd=cwd, capture_output=True, text=True, timeout=30)
        if p.returncode == 0:
            return ("passed", p.stdout)
        if p.returncode in (4, 5):
            return ("missing", p.stdout)
        if p.returncode == 1:
            return ("failed", p.stdout)
        return ("missing", p.stdout)


CHAMP = {"working": 0.8, "held_out": 0.7}


def _g(working=0.85, held_out=0.7):
    return {"working": working, "held_out": held_out, "held_out_measured": True,
           "divergence_alarm": False, "safety_flag": False}


def _grade(*values):
    it = iter(values)
    return lambda repo: next(it)


def _run(ad, *, cand_repo, changed, require_test=True, red_proof=True,
         base_repo="/basewt", base_sha="basesha1", grade_values=None):
    grade_values = grade_values or (_g(), _g())
    return code_round.run_code_round(
        adapter=ad, main_repo="/main", cand_repo=cand_repo, branch="cand",
        changed_paths=changed, champion_scores=CHAMP, grade_fn=_grade(*grade_values),
        label="cand", require_test=require_test, red_proof=red_proof,
        base_repo=base_repo, base_sha=base_sha)


def _worktree_calls(ad):
    return [c for c in ad.calls if isinstance(c, tuple) and c[0] == "add_worktree_detached"]


def _named_test_calls(ad):
    return [c for c in ad.calls if isinstance(c, tuple) and c[0] == "run_named_test"]


# -- gating: red_proof / require_test / base_repo+base_sha -------------------------------
def test_red_proof_off_never_touches_the_worktree_seam(tmp_path):
    cand = tmp_path / "cand"
    _write(str(cand), "tests/test_x.py", "def test_x():\n    assert True\n")
    ad = RealPytestAdapter()
    res = _run(ad, cand_repo=str(cand), changed=["src/x.py", "tests/test_x.py"], red_proof=False)
    assert res["action"] == "merged"
    assert _worktree_calls(ad) == []


def test_require_test_off_skips_red_proof_even_when_the_knob_is_on(tmp_path):
    cand = tmp_path / "cand"
    _write(str(cand), "tests/test_x.py", "def test_x():\n    assert True\n")
    ad = RealPytestAdapter()
    res = _run(ad, cand_repo=str(cand), changed=["src/x.py", "tests/test_x.py"],
              require_test=False, red_proof=True)
    assert res["action"] == "merged"
    assert _worktree_calls(ad) == []


def test_missing_base_repo_or_sha_skips_silently_never_crashes(tmp_path):
    cand = tmp_path / "cand"
    _write(str(cand), "tests/test_x.py", "def test_x():\n    assert True\n")
    ad = RealPytestAdapter()
    res = _run(ad, cand_repo=str(cand), changed=["src/x.py", "tests/test_x.py"],
              base_repo=None, base_sha=None)
    assert res["action"] == "merged"
    assert _worktree_calls(ad) == []


def test_no_changed_test_file_skips_the_worktree_seam(tmp_path):
    cand = tmp_path / "cand"
    ad = RealPytestAdapter()
    res = _run(ad, cand_repo=str(cand), changed=["docs/readme.md"])
    assert res["action"] == "merged"
    assert _worktree_calls(ad) == []


# -- F6's own bug: modifying/extending an EXISTING file -----------------------------------
def test_appending_a_new_case_to_an_existing_file_is_never_wrongly_discarded(tmp_path):
    """THE bug: the base file has an unrelated, always-passing test; the candidate ADDS a
    new test case alongside it. File-level red-proof would have run the WHOLE base file
    (only the old, passing test exists there) and wrongly concluded 'already passes'."""
    cand = tmp_path / "cand"
    _write(str(cand), "tests/test_x.py", """\
        def test_existing():
            assert 1 == 1

        def test_new_case():
            assert 2 + 2 == 4
        """)
    ad = RealPytestAdapter(base_files={
        "tests/test_x.py": "def test_existing():\n    assert 1 == 1\n"})
    res = _run(ad, cand_repo=str(cand), changed=["src/x.py", "tests/test_x.py"])
    assert res["action"] == "merged"
    # ONLY the new node was checked — the unrelated, unchanged test_existing never ran
    checked = [c[2] for c in _named_test_calls(ad)]
    assert checked == ["tests/test_x.py::test_new_case"]


def test_modifying_an_existing_case_in_place_checks_only_that_node(tmp_path):
    """The other normal shape: an EXISTING assertion is corrected. Same node NAME, new
    BODY — must be recognized as changed and checked (against the base's OLD body)."""
    cand = tmp_path / "cand"
    _write(str(cand), "tests/test_x.py", """\
        def test_unrelated():
            assert 1 == 1

        def test_foo():
            assert 1 == 1
        """)
    ad = RealPytestAdapter(base_files={"tests/test_x.py": textwrap.dedent("""\
        def test_unrelated():
            assert 1 == 1

        def test_foo():
            assert 1 == 2
        """)})
    res = _run(ad, cand_repo=str(cand), changed=["src/x.py", "tests/test_x.py"])
    assert res["action"] == "merged"          # base's OLD test_foo body (assert 1==2) fails
    checked = [c[2] for c in _named_test_calls(ad)]
    assert checked == ["tests/test_x.py::test_foo"]   # test_unrelated never touched


def test_an_unchanged_node_in_a_touched_file_is_never_checked(tmp_path):
    """A file can be in `changed_paths` (e.g. a docstring/import tweak) with every actual
    test body byte-identical to base — nothing to red-proof, must not even attempt it."""
    same = "def test_a():\n    assert True\n\ndef test_b():\n    assert True\n"
    cand = tmp_path / "cand"
    _write(str(cand), "tests/test_x.py", same)
    ad = RealPytestAdapter(base_files={"tests/test_x.py": same})
    res = _run(ad, cand_repo=str(cand), changed=["src/x.py", "tests/test_x.py"])
    assert res["action"] == "merged"
    assert _named_test_calls(ad) == []


def test_deleting_an_obsolete_test_file_is_never_discarded(tmp_path):
    """A test file present on base, ABSENT from the candidate (deleted) — a legitimate
    maintenance action, never something that needs to 'discriminate'."""
    cand = tmp_path / "cand"
    # tests/test_old.py deliberately NOT written into cand — it was deleted
    _write(str(cand), "src/x.py", "x = 1\n")
    ad = RealPytestAdapter(base_files={
        "tests/test_old.py": "def test_deprecated():\n    assert True\n"})
    res = _run(ad, cand_repo=str(cand), changed=["src/x.py", "tests/test_old.py"],
              require_test=False)   # a pure deletion+source change; acceptance gate is a
    assert res["action"] == "merged"           # separate concern, disabled here to isolate red-proof
    assert _named_test_calls(ad) == []


def test_a_brand_new_test_file_is_satisfied_without_running_anything(tmp_path):
    """A whole NEW file (never existed on base) — every node is absent from base by
    construction; still gets a confirming run (defense in depth), which resolves 'missing'."""
    cand = tmp_path / "cand"
    _write(str(cand), "tests/test_new.py", "def test_brand_new():\n    assert True\n")
    ad = RealPytestAdapter()   # no base_files at all — the whole file is absent on base
    res = _run(ad, cand_repo=str(cand), changed=["src/x.py", "tests/test_new.py"])
    assert res["action"] == "merged"
    checked = [c[2] for c in _named_test_calls(ad)]
    assert checked == ["tests/test_new.py::test_brand_new"]


# -- the discriminating gate itself ---------------------------------------------------------
def test_a_changed_node_that_already_passes_on_base_discards(tmp_path):
    cand = tmp_path / "cand"
    _write(str(cand), "tests/test_x.py", "def test_foo():\n    assert True\n")
    ad = RealPytestAdapter(base_files={"tests/test_x.py": "def test_foo():\n    assert True\n"})
    # force a "changed" classification by making the CANDIDATE body textually different
    # (a comment) while still trivially passing on base — proves the GATE (not just the
    # targeting) still fires when a genuinely-checked node doesn't discriminate.
    _write(str(cand), "tests/test_x.py", "def test_foo():\n    # tweaked\n    assert True\n")
    res = _run(ad, cand_repo=str(cand), changed=["src/x.py", "tests/test_x.py"])
    assert res["action"] == "discarded" and res["stage"] == "no_test"
    assert "test passes on the pristine base" in res["why"]
    assert "tests/test_x.py::test_foo" in res["why"]
    assert "run_tests" not in ad.calls            # never reached the (expensive) suite gate


def test_worktree_is_always_cleaned_up_on_discard_and_on_proceed(tmp_path):
    cand_discard = tmp_path / "cand_discard"
    _write(str(cand_discard), "tests/test_x.py", "def test_foo():\n    # v2\n    assert True\n")
    ad_discard = RealPytestAdapter(base_files={"tests/test_x.py": "def test_foo():\n    assert True\n"})
    _run(ad_discard, cand_repo=str(cand_discard), changed=["src/x.py", "tests/test_x.py"])
    assert any(c[0] == "remove_worktree" for c in ad_discard.calls if isinstance(c, tuple))

    cand_proceed = tmp_path / "cand_proceed"
    _write(str(cand_proceed), "tests/test_x.py", "def test_new():\n    assert True\n")
    ad_proceed = RealPytestAdapter()
    _run(ad_proceed, cand_repo=str(cand_proceed), changed=["src/x.py", "tests/test_x.py"])
    assert any(c[0] == "remove_worktree" for c in ad_proceed.calls if isinstance(c, tuple))


def test_fail_fast_stops_at_the_first_non_discriminating_node(tmp_path):
    cand = tmp_path / "cand"
    _write(str(cand), "tests/test_a.py", "def test_a():\n    assert 1 == 1\n")   # new -> missing on base
    _write(str(cand), "tests/test_b.py", "def test_b():\n    # v2\n    assert True\n")  # changed, passes on base
    _write(str(cand), "tests/test_c.py", "def test_c():\n    assert 1 == 1\n")   # never reached
    ad = RealPytestAdapter(base_files={"tests/test_b.py": "def test_b():\n    assert True\n"})
    res = _run(ad, cand_repo=str(cand),
              changed=["src/x.py", "tests/test_a.py", "tests/test_b.py", "tests/test_c.py"])
    assert res["action"] == "discarded" and res["stage"] == "no_test"
    assert "tests/test_b.py::test_b" in res["why"]
    checked = [c[2] for c in _named_test_calls(ad)]
    assert "tests/test_c.py::test_c" not in checked      # never reached


def test_only_test_classified_changed_paths_are_checked(tmp_path):
    cand = tmp_path / "cand"
    _write(str(cand), "src/x.py", "x = 1\n")
    _write(str(cand), "src/y.py", "y = 1\n")
    _write(str(cand), "tests/test_x.py", "def test_x():\n    assert True\n")
    ad = RealPytestAdapter()
    res = _run(ad, cand_repo=str(cand), changed=["src/x.py", "src/y.py", "tests/test_x.py"])
    assert res["action"] == "merged"
    checked = [c[2] for c in _named_test_calls(ad)]
    assert checked == ["tests/test_x.py::test_x"]        # src/x.py, src/y.py never sent through


def test_worktree_built_at_the_recorded_base_sha_in_base_repo(tmp_path):
    cand = tmp_path / "cand"
    _write(str(cand), "tests/test_x.py", "def test_x():\n    assert True\n")
    ad = RealPytestAdapter()
    _run(ad, cand_repo=str(cand), changed=["src/x.py", "tests/test_x.py"],
        base_repo="/dev/clone", base_sha="deadbeef")
    (repo, dest, sha), = [c[1:] for c in _worktree_calls(ad)]
    assert repo == "/dev/clone" and sha == "deadbeef"


def test_red_proof_runs_before_the_full_test_suite(tmp_path):
    """Fail fast, BEFORE the (expensive) full suite gate."""
    cand = tmp_path / "cand"
    _write(str(cand), "tests/test_x.py", "def test_foo():\n    # v2\n    assert True\n")
    ad = RealPytestAdapter(base_files={"tests/test_x.py": "def test_foo():\n    assert True\n"})
    _run(ad, cand_repo=str(cand), changed=["src/x.py", "tests/test_x.py"])
    assert "run_tests" not in ad.calls


# -- F6: cap + fail-open telemetry ----------------------------------------------------------
def test_caps_the_number_of_nodes_checked_and_notes_it(tmp_path):
    cand = tmp_path / "cand"
    n = code_round.MAX_RED_PROOF_NODES + 5
    body = "\n".join(f"def test_n{i}():\n    assert True\n" for i in range(n))
    _write(str(cand), "tests/test_many.py", body)     # ALL brand new -> every node 'missing' on base
    ad = RealPytestAdapter()
    res = _run(ad, cand_repo=str(cand), changed=["src/x.py", "tests/test_many.py"])
    assert res["action"] == "merged"
    assert len(_named_test_calls(ad)) == code_round.MAX_RED_PROOF_NODES
    assert any("capped at" in note for note in res.get("red_proof_skipped", []))


def test_all_missing_is_telemetized_never_a_discard(tmp_path):
    cand = tmp_path / "cand"
    _write(str(cand), "tests/test_new.py", "def test_only_new():\n    assert True\n")
    ad = RealPytestAdapter()      # no base_files -> the node is 'missing' on base
    res = _run(ad, cand_repo=str(cand), changed=["src/x.py", "tests/test_new.py"])
    assert res["action"] == "merged"
    assert res.get("red_proof_all_missing") is True


def test_unparseable_candidate_file_is_skipped_not_discarded_or_crashed(tmp_path):
    cand = tmp_path / "cand"
    _write(str(cand), "tests/test_broken.py", "def test_x(:\n    this is not python\n")
    ad = RealPytestAdapter()
    res = _run(ad, cand_repo=str(cand), changed=["src/x.py", "tests/test_broken.py"])
    assert res["action"] == "merged"              # never crashes, never a false discard
    assert _named_test_calls(ad) == []
    assert any("AST parse" in note or "skipped" in note
              for note in res.get("red_proof_skipped", []))
