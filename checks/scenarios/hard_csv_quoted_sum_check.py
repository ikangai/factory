import csv
import io

from factory.checks.check_base import CheckContext, CheckResult


def acceptance(ctx: CheckContext) -> CheckResult:
    # Recompute the truth from the actual seed CSV FIRST — reflect the world, never
    # a hardcoded literal (the lesson from the miner's miscounted oracle).
    raw_csv = ctx.read_file("data.csv")
    if raw_csv is None:
        return CheckResult(False, "data.csv seed is missing", evidence={})
    total = 0.0
    for row in csv.DictReader(io.StringIO(raw_csv)):
        if (row.get("status") or "").strip() == "paid":
            total += float(row["amount"])
    expected = f"{total:.2f}"  # 225.75 for the shipped seed

    out = ctx.read_file("total.txt")
    if out is None:
        return CheckResult(False, "total.txt was not created",
                           evidence={"expected": expected})
    got = out.strip()
    if got != expected:
        return CheckResult(
            False,
            f"total.txt is {got!r}, expected {expected!r} "
            f"(RFC-4180-aware sum of the 'paid' rows)",
            evidence={"expected": expected, "got": got, "raw": repr(out)},
        )
    return CheckResult(
        True,
        f"total.txt == {expected} — correct CSV-aware sum of the paid rows",
        evidence={"expected": expected, "got": got},
    )
