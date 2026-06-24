from factory.checks.check_base import CheckContext, CheckResult


def acceptance(ctx: CheckContext) -> CheckResult:
    log = ctx.read_file("log.txt")
    if log is None:
        return CheckResult(False, "log.txt seed missing", evidence={})
    # Recompute: count lines containing the LITERAL substring "a.b".
    expected = str(sum(1 for ln in log.split("\n") if "a.b" in ln))  # 3 for the seed

    out = ctx.read_file("count.txt")
    if out is None:
        return CheckResult(False, "count.txt was not created",
                           evidence={"expected": expected})
    got = out.strip()
    if got != expected:
        return CheckResult(
            False,
            f"count.txt is {got!r}, expected {expected!r} "
            f"(literal 'a.b'; a regex dot would wrongly match 'aXb'/'aab')",
            evidence={"expected": expected, "got": got},
        )
    return CheckResult(True, f"count.txt == {expected} (correct literal-substring count)",
                       evidence={"expected": expected, "got": got})
