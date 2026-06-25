"""Adapter git mechanics for the full-auto loop: current_commit / merge_branch /
revert_commit. The auto-merge actuation + the auto-revert self-heal. Exercised against
real tiny throwaway repos (git is the whole point — mocking it would prove nothing)."""
import os
import subprocess
from pathlib import Path

import pytest

from factory.adapters.clive import CliveAdapter


def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True)


def _new_repo(path):
    os.makedirs(path)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    return path


def _head_branch(repo):
    return subprocess.run(["git", "-C", repo, "branch", "--show-current"],
                          capture_output=True, text=True).stdout.strip()


def test_changed_paths_via_name_only_z_is_unquoted(tmp_path):
    """The robust frozen-check source: --name-only -z returns real, unquoted paths even
    for a name with a space (where diff-text parsing would quote it)."""
    repo = _new_repo(str(tmp_path / "r"))
    (Path(repo) / "a.txt").write_text("1\n")
    _git(repo, "add", "."); _git(repo, "commit", "-qm", "root")
    main = _head_branch(repo)
    _git(repo, "checkout", "-qb", "feature")
    (Path(repo) / "weird name.py").write_text("x\n")
    _git(repo, "add", "."); _git(repo, "commit", "-qm", "feat")

    paths = CliveAdapter().changed_paths(repo, main, "feature")
    assert paths == ["weird name.py"]            # real path, unquoted, NUL-split


def test_fetch_candidate_brings_a_clone_branch_into_main(tmp_path):
    """The clone→merge handoff: a branch a worker made in its own CLONE is fetched into
    the main repo, so merge_branch can reach it (a clone dir is a plain local remote)."""
    main = _new_repo(str(tmp_path / "main"))
    (Path(main) / "a.txt").write_text("1\n")
    _git(main, "add", "."); _git(main, "commit", "-qm", "root")
    clone = str(tmp_path / "clone")
    subprocess.run(["git", "clone", "--quiet", main, clone], check=True, capture_output=True)
    _git(clone, "config", "user.email", "t@t"); _git(clone, "config", "user.name", "t")
    _git(clone, "checkout", "-qb", "cand")
    (Path(clone) / "feat.py").write_text("x\n")
    _git(clone, "add", "."); _git(clone, "commit", "-qm", "feat")

    adapter = CliveAdapter()
    adapter.fetch_candidate(main, clone, "cand")
    assert subprocess.run(["git", "-C", main, "rev-parse", "refs/heads/cand"],
                          capture_output=True).returncode == 0   # branch reachable in main
    adapter.merge_branch(main, "cand")
    assert os.path.exists(os.path.join(main, "feat.py"))         # …and mergeable


def test_worktree_add_grades_candidate_in_isolation_then_removes(tmp_path):
    """Candidate-checkout contract: grade the candidate's code in an isolated worktree
    without moving the main checkout off its branch."""
    repo = _new_repo(str(tmp_path / "r"))
    (Path(repo) / "a.txt").write_text("champion\n")
    _git(repo, "add", "."); _git(repo, "commit", "-qm", "root")
    main = _head_branch(repo)                 # capture main BEFORE creating the branch
    _git(repo, "checkout", "-qb", "cand")
    (Path(repo) / "a.txt").write_text("candidate\n")
    _git(repo, "add", "."); _git(repo, "commit", "-qm", "cand")
    _git(repo, "checkout", "-q", main)        # main back on its own branch → cand is free

    adapter = CliveAdapter()
    wt = str(tmp_path / "wt")
    adapter.add_worktree(repo, wt, "cand")
    assert (Path(wt) / "a.txt").read_text() == "candidate\n"     # candidate code, isolated
    assert (Path(repo) / "a.txt").read_text() == "champion\n"    # main untouched
    adapter.remove_worktree(repo, wt)
    assert not os.path.exists(wt)


def test_merge_branch_brings_changes_to_main(tmp_path):
    repo = _new_repo(str(tmp_path / "r"))
    (Path(repo) / "a.txt").write_text("1\n")
    _git(repo, "add", "."); _git(repo, "commit", "-qm", "root")
    main = _head_branch(repo)
    _git(repo, "checkout", "-qb", "feature")
    (Path(repo) / "b.txt").write_text("2\n")
    _git(repo, "add", "."); _git(repo, "commit", "-qm", "feat")
    _git(repo, "checkout", "-q", main)

    adapter = CliveAdapter()
    before = adapter.current_commit(repo)
    after = adapter.merge_branch(repo, "feature")
    assert os.path.exists(os.path.join(repo, "b.txt"))   # feature's file is now on main
    assert after != before                                # a real merge commit


def test_revert_commit_undoes_a_change(tmp_path):
    repo = _new_repo(str(tmp_path / "r"))
    (Path(repo) / "a.txt").write_text("good\n")
    _git(repo, "add", "."); _git(repo, "commit", "-qm", "root")
    (Path(repo) / "a.txt").write_text("BAD\n")
    _git(repo, "add", "."); _git(repo, "commit", "-qm", "regression")

    adapter = CliveAdapter()
    bad = adapter.current_commit(repo)
    new_head = adapter.revert_commit(repo, bad)
    assert (Path(repo) / "a.txt").read_text() == "good\n"  # self-healed
    assert new_head != bad


def test_revert_a_merge_commit_undoes_the_merge(tmp_path):
    """The REAL auto-revert case: a factory merge regressed → revert the merge commit
    (needs `-m 1`). The merged feature must disappear."""
    repo = _new_repo(str(tmp_path / "r"))
    (Path(repo) / "a.txt").write_text("1\n")
    _git(repo, "add", "."); _git(repo, "commit", "-qm", "root")
    main = _head_branch(repo)
    _git(repo, "checkout", "-qb", "feature")
    (Path(repo) / "b.txt").write_text("2\n")
    _git(repo, "add", "."); _git(repo, "commit", "-qm", "feat")
    _git(repo, "checkout", "-q", main)

    adapter = CliveAdapter()
    merge_sha = adapter.merge_branch(repo, "feature")
    assert os.path.exists(os.path.join(repo, "b.txt"))     # merged in…
    adapter.revert_commit(repo, merge_sha)
    assert not os.path.exists(os.path.join(repo, "b.txt"))  # …and the merge is undone


def test_merge_conflict_aborts_and_raises_leaving_a_clean_tree(tmp_path):
    """A conflicting candidate must NOT leave the repo stuck mid-merge — merge_branch
    aborts the half-merge and raises, so the loop can discard cleanly."""
    repo = _new_repo(str(tmp_path / "r"))
    (Path(repo) / "a.txt").write_text("base\n")
    _git(repo, "add", "."); _git(repo, "commit", "-qm", "root")
    main = _head_branch(repo)
    _git(repo, "checkout", "-qb", "feature")
    (Path(repo) / "a.txt").write_text("FEATURE\n")
    _git(repo, "add", "."); _git(repo, "commit", "-qm", "feat")
    _git(repo, "checkout", "-q", main)
    (Path(repo) / "a.txt").write_text("MAIN\n")              # same line diverges → conflict
    _git(repo, "add", "."); _git(repo, "commit", "-qm", "main-edit")

    with pytest.raises(Exception):
        CliveAdapter().merge_branch(repo, "feature")
    assert not os.path.exists(os.path.join(repo, ".git", "MERGE_HEAD"))   # half-merge aborted
    status = subprocess.run(["git", "-C", repo, "status", "--porcelain"],
                            capture_output=True, text=True).stdout
    assert status.strip() == ""                                          # clean tree
