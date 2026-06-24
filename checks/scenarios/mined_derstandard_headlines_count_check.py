from factory.checks.check_base import CheckContext, CheckResult


def acceptance(ctx: CheckContext) -> CheckResult:
    expected_lines = [
        "Nationalrat beschliesst neues Budget",
        "Wetter: Hitzewelle erreicht Wien",
        "Champions League: Auslosung am Freitag",
    ]
    expected_count = len(expected_lines)

    # --- count.txt ---
    count_raw = ctx.read_file("count.txt")
    if count_raw is None:
        return CheckResult(False, "count.txt is missing", evidence={"count_raw": None})
    count_trimmed = count_raw.strip()
    if count_trimmed == "":
        return CheckResult(
            False,
            "count.txt is empty",
            evidence={"count_raw": repr(count_raw)},
        )
    if count_trimmed != str(expected_count):
        return CheckResult(
            False,
            f"count.txt trimmed is {count_trimmed!r}, expected {str(expected_count)!r}",
            evidence={"count_raw": repr(count_raw), "count_trimmed": count_trimmed},
        )

    # --- headlines.txt ---
    headlines_raw = ctx.read_file("headlines.txt")
    if headlines_raw is None:
        return CheckResult(
            False,
            "headlines.txt is missing",
            evidence={"headlines_raw": None, "count_trimmed": count_trimmed},
        )

    # Split into lines; tolerate trailing newline/whitespace but be strict on content.
    # Strip a single trailing newline-block, then split on newlines.
    body = headlines_raw.rstrip("\n")
    if body == "":
        return CheckResult(
            False,
            "headlines.txt is empty (or only whitespace/newlines)",
            evidence={"headlines_raw": repr(headlines_raw)},
        )
    lines = body.split("\n")
    # Tolerate trailing whitespace on each line only.
    stripped_lines = [ln.rstrip() for ln in lines]

    if stripped_lines != expected_lines:
        return CheckResult(
            False,
            "headlines.txt does not match the expected three lines in order",
            evidence={
                "expected_lines": expected_lines,
                "actual_lines": stripped_lines,
                "headlines_raw": repr(headlines_raw),
            },
        )

    return CheckResult(
        True,
        "count.txt trimmed equals '3' and headlines.txt contains exactly the three "
        "teaser-title headlines in document order (sidebar-title excluded)",
        evidence={
            "count_trimmed": count_trimmed,
            "headlines": stripped_lines,
            "headlines_raw": repr(headlines_raw),
        },
    )
