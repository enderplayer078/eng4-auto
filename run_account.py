"""CLI and programmatic runner for Cambridge One automated exercise completion."""

from __future__ import annotations

import argparse
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


def run_for_account(
    username: str,
    password: str,
    scope: str = "assignments",
    units: list[int] | None = None,
    limit: int | None = None,
    include_completed: bool = False,
    headless: bool = True,
    quiet_logs: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, int]:
    """Log into Cambridge One and complete all target activities at 100%."""
    session_dir = build_session_dir(username)
    session: CambridgeSession | None = None

    completed = 0
    skipped = 0
    total = 0

    try:
        session = CambridgeSession(
            username=username,
            headless=headless,
            quiet_logs=quiet_logs,
            session_dir=session_dir,
        )
        driver = session.start()
        session.login(password)

        targets = session.discover_course_targets(
            scope=scope,
            units=units,
            include_completed=include_completed,
        )
        if limit is not None and limit > 0:
            targets = targets[:limit]

        total = len(targets)
        if not quiet_logs:
            print(f"[runner] Found {total} target activities (scope={scope})")
        else:
            print(f"[runner] Queued {total} activities")

        if on_progress:
            on_progress(0, total)

        for idx, target in enumerate(targets, start=1):
            result = solve_activity(
                driver=driver,
                activity_url=target.activity_url,
                title="" if quiet_logs else f"Unit {target.unit_num} {target.lesson}: {target.title}",
                quiet_logs=quiet_logs,
            )
            if result is None:
                skipped += 1
            else:
                completed += 1

            if on_progress:
                on_progress(idx, total)

            if quiet_logs:
                print(f"[runner] Progress: {idx}/{total}")
            else:
                print(
                    f"[runner] Progress: {idx}/{total} "
                    f"(completed={completed}, skipped={skipped})"
                )

        return {
            "total": total,
            "completed": completed,
            "skipped": skipped,
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

    args = parser.parse_args(argv)
    units = _parse_units(args.units)

    summary = run_for_account(
        username=args.username,
        password=args.password,
        scope=args.scope,
        units=units,
        limit=args.limit,
        include_completed=args.include_completed,
        headless=args.headless,
        quiet_logs=args.quiet,
    )
    print(
        f"[runner] Done: completed={summary['completed']}, "
        f"skipped={summary['skipped']}, total={summary['total']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
