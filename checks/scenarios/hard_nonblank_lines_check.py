from factory.checks.check_base import CheckContext, CheckResult


def acceptance(ctx: CheckContext) -> CheckResult:
    data = ctx.read_file("lines.txt")
    if data is None:
        return CheckResult(False, "lines.txt seed missing", evidence={})
    # Recompute: a line counts only if it has non-whitespace content. split("\n")
    # yields a trailing "" for the final newline, which .strip() drops correctly.
    expected = str(sum(1 for ln in data.split("\n") if ln.strip()))  # 3 for the seed

    out = ctx.read_file("count.txt")
    if out is None:
        return CheckResult(False, "count.txt was not created",
                           evidence={"expected": expected})
    got = out.strip()
    if got != expected:
        return CheckResult(
            False,
            f"count.txt is {got!r}, expected {expected!r} "
            f"(whitespace-only lines count as empty)",
            evidence={"expected": expected, "got": got},
        )
    return CheckResult(True, f"count.txt == {expected} (correct non-blank line count)",
                       evidence={"expected": expected, "got": got})
