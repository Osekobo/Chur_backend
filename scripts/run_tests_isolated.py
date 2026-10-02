"""Run each approvals/audit test in its own process, with a timeout.

The whole file hung once and a single 30-test file gives no clue which test is
responsible. Running them one at a time isolates it, and the timeout stops a
single stuck test from blocking the whole sweep.
"""

from __future__ import annotations

import subprocess
import sys

TIMEOUT_SECONDS = 45

# pyproject sets addopts = "-q", which makes --collect-only print only a count
# instead of node ids, so addopts is cleared for the collection run.
collect = subprocess.run(
    [
        sys.executable,
        "-m",
        "pytest",
        "tests/test_approvals_audit.py",
        "-o",
        "addopts=",
        "--collect-only",
        "-q",
    ],
    capture_output=True,
    text=True,
    check=False,
)
test_ids = [
    line.strip()
    for line in collect.stdout.splitlines()
    if line.strip().startswith("tests/test_approvals_audit.py::")
]

if not test_ids:
    print("no tests collected")
    sys.exit(1)

failed: list[str] = []
hung: list[str] = []
passed = 0

for test_id in test_ids:
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                test_id,
                "-q",
                "--no-header",
                "-p",
                "no:cacheprovider",
            ],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        hung.append(test_id)
        print(f"HANG   {test_id}")
        continue
    if result.returncode == 0:
        passed += 1
        print(f"pass   {test_id}")
    else:
        failed.append(test_id)
        tail = [line for line in result.stdout.splitlines() if line.strip()][-1:]
        print(f"FAIL   {test_id}\n         {tail[0] if tail else ''}")

print()
print(f"passed={passed} failed={len(failed)} hung={len(hung)}")
if hung:
    print("hung tests:")
    for test_id in hung:
        print("  ", test_id)
if failed:
    print("failed tests:")
    for test_id in failed:
        print("  ", test_id)
sys.exit(1 if (failed or hung) else 0)
