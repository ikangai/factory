"""Adapter git mechanics for the full-auto loop: current_commit / merge_branch /
revert_commit. The auto-merge actuation + the auto-revert self-heal. Exercised against
real tiny throwaway repos (git is the whole point — mocking it would prove nothing)."""
import os
import subprocess
from pathlib import Path

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
