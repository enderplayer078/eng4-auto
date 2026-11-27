"""CLI and programmatic runner for Cambridge One automated exercise completion."""

from __future__ import annotations

import argparse
import random
import shutil
import sys
from typing import Callable

from account_session import CambridgeSession, build_session_dir
from solver import solve_activity


def _parse_units(raw_units: list[str] | None) -> list[int] | None:
    if not raw_units:
        return None
    units: list[int] = []
    for chunk in raw_units:
        for part in chunk.split(","):
            part = part.strip()
            if not part:
                continue
            val = int(part)
            if val < 1 or val > 10:
                raise ValueError(f"Unit number must be between 1 and 10, got {val}")
            if val not in units:
                units.append(val)
    return sorted(units) if units else None


def _build_activity_budgets(
    targets: list,
    min_total_minutes: float,
    max_total_minutes: float,
    total_course_count: int = 40,
) -> list[float]:
    """Allocate randomized per-activity pacing budgets scaled proportionally to course size."""
    count = len(targets)
    if count == 0 or max_total_minutes <= 0:
        return [0.0] * count

    low = max(0.0, min(min_total_minutes, max_total_minutes))
    high = max(low, max_total_minutes)
    target_full_sec = random.uniform(low, high) * 60.0
    # Scale proportionally relative to the full course activity count
    denom = float(max(1, total_course_count))
    scaled_total_sec = target_full_sec * (count / denom)
    # Cambridge One records active time in 10s floor ticks (accTimeUnit=10) only
    # after the activity iframe launches (~3.5s active solve/check overhead vs
    # ~5.0s average floor quantization loss -> +1.5s compensation per activity).
    pacing_pool_sec = max(0.0, scaled_total_sec + (count * 1.5))
    if pacing_pool_sec <= 0:
        return [0.0] * count

    weights: list[float] = []
    for t in targets:
        base_w = 1.35 if getattr(t, "lesson", "") == "D" else 1.0
        weights.append(base_w * random.uniform(0.8, 1.2))
    weight_sum = sum(weights) or 1.0
    return [pacing_pool_sec * (w / weight_sum) for w in weights]


def run_for_account(
    username: str,
    password: str,
    scope: str = "assignments",
    units: list[int] | None = None,
    limit: int | None = None,
    max_units: int = 0,
    max_activities: int = 0,
    include_completed: bool = False,
    headless: bool = True,
    quiet_logs: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
    min_total_minutes: float = 20.0,
    max_total_minutes: float = 40.0,
) -> dict[str, int | bool]:
    """Log into Cambridge One and complete target activities at 100%."""
    session_dir = build_session_dir(username)
    session: CambridgeSession | None = None

    completed = 0
    skipped = 0

    try:
        session = CambridgeSession(
            username=username,
            headless=headless,
            quiet_logs=quiet_logs,
            session_dir=session_dir,
        )
        driver = session.start()
        session.login(password)

        all_targets = session.discover_course_targets(
            scope=scope,
            units=units,
            include_completed=True,
        )
        total_all = len(all_targets)
        if include_completed:
            already_done = 0
            remaining_targets = list(all_targets)
        else:
            already_done = sum(1 for t in all_targets if t.already_100)
            remaining_targets = [t for t in all_targets if not t.already_100]

        selected = list(remaining_targets)
        if max_units > 0 and selected:
            ordered_unit_nums: list[int] = []
            for t in selected:
                if t.unit_num not in ordered_unit_nums:
                    ordered_unit_nums.append(t.unit_num)
            allowed_units = set(ordered_unit_nums[:max_units])
            selected = [t for t in selected if t.unit_num in allowed_units]

        if max_activities > 0:
            selected = selected[:max_activities]
        if limit is not None and limit > 0:
            selected = selected[:limit]

        is_partial_selection = len(selected) < len(remaining_targets)
        progress_total = total_all if total_all > 0 else len(selected)
        run_count = len(selected)

        if not quiet_logs:
            print(
                f"[runner] Found {run_count} target activities "
                f"(already_done={already_done}, total={progress_total}, scope={scope})"
            )
        else:
            print(f"[runner] Queued {run_count} activities (total={progress_total})")

        if on_progress:
            on_progress(already_done, progress_total)

        budgets = _build_activity_budgets(
            selected,
            min_total_minutes,
            max_total_minutes,
            total_course_count=progress_total or 40,
        )

        for idx, (target, budget_sec) in enumerate(zip(selected, budgets), start=1):
            result = solve_activity(
                driver=driver,
                activity_url=target.activity_url,
                title="" if quiet_logs else f"Unit {target.unit_num} {target.lesson}: {target.title}",
                quiet_logs=quiet_logs,
                activity_budget_sec=budget_sec,
            )
            if result is None:
                skipped += 1
            else:
                completed += 1

            overall_done = min(progress_total, already_done + idx)
            if on_progress:
                on_progress(overall_done, progress_total)

            if quiet_logs:
                print(f"[runner] Progress: {overall_done}/{progress_total}")
            else:
                print(
                    f"[runner] Progress: {overall_done}/{progress_total} "
                    f"(completed={completed}, skipped={skipped})"
                )

        final_done = min(progress_total, already_done + completed)
        all_completed = (not is_partial_selection) and (skipped == 0) and (final_done >= progress_total)
        return {
            "total": progress_total,
            "completed": final_done,
            "ran_this_session": completed,
            "skipped": skipped,
            "all_completed": all_completed,
            "partial": not all_completed,
        }
    finally:
        if session is not None:
            session.close()
        shutil.rmtree(session_dir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Automated 100%% Cambridge One exercise runner (ENGLISH 4)."
    )
    parser.add_argument("--username", required=True, help="Cambridge One username/email")
    parser.add_argument("--password", required=True, help="Cambridge One password")
    parser.add_argument(
        "--scope",
        choices=["assignments", "workbook"],
        default="assignments",
        help="Target scope (default: assignments = Homework Eng3 Units 6-9)",
    )
    parser.add_argument(
        "--units",
        nargs="+",
        help="Optional unit numbers (e.g. --units 6 7 8 9 or --units 1,2,3)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional max number of activities to run",
    )
    parser.add_argument(
        "--max-units",
        type=int,
        default=0,
        help="Optional max number of incomplete units to process (0 = all)",
    )
    parser.add_argument(
        "--max-activities",
        type=int,
        default=0,
        help="Optional max number of activities to process (0 = all)",
    )
    parser.add_argument(
        "--include-completed",
        action="store_true",
        help="Also re-run activities already marked 100%%",
    )
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run Chrome in headless mode (default: True)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress activity titles and verbose logs",
    )
    parser.add_argument(
        "--min-minutes",
        type=float,
        default=20.0,
        help="Minimum randomized total pacing time in minutes for 40 items (0 to disable)",
    )
    parser.add_argument(
        "--max-minutes",
        type=float,
        default=40.0,
        help="Maximum randomized total pacing time in minutes for 40 items (0 to disable)",
    )

    args = parser.parse_args(argv)
    units = _parse_units(args.units)

    summary = run_for_account(
        username=args.username,
        password=args.password,
        scope=args.scope,
        units=units,
        limit=args.limit,
        max_units=max(0, args.max_units),
        max_activities=max(0, args.max_activities),
        include_completed=args.include_completed,
        headless=args.headless,
        quiet_logs=args.quiet,
        min_total_minutes=args.min_minutes,
        max_total_minutes=args.max_minutes,
    )
    print(
        f"[runner] Done: completed={summary['completed']}, "
        f"skipped={summary['skipped']}, total={summary['total']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
