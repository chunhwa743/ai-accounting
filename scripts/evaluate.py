#!/usr/bin/env python
"""Score a processed client against the answer key.

    python scripts/evaluate.py                 # offline provider
    python scripts/evaluate.py --live          # real model

The number that matters is auto-post precision: of the transactions posted
without anyone looking, how many were actually right. Overall accuracy is
easier to reach and less useful - a system that flags everything scores well on
accuracy while saving nobody any time.

The calibration table answers the other question: does a confidence of 0.9
actually mean about 90% correct? If it does not, the score is decoration.

The answer key lives in the Account, Tax and "Why this is hard" columns of
data/testdata/*.md, and is read only here. The pipeline never sees it.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from aiacct.config import get_settings  # noqa: E402

CLIENT_UEN = "202512345A"
BANDS = [
    (0.90, 1.01, "0.90 - 1.00"),
    (0.75, 0.90, "0.75 - 0.90"),
    (0.60, 0.75, "0.60 - 0.75"),
    (0.00, 0.60, "below 0.60"),
]


def band_for(confidence: float) -> str:
    for low, high, label in BANDS:
        if low <= confidence < high:
            return label
    return "below 0.60"


def evaluate(repos, run_id: int, truth: dict) -> dict:
    """Compare one run's allocations against the answer key."""
    from demo_learning_loop import lookup

    from aiacct.models import AllocationStatus, DecisionMethod

    rows = []
    for allocation in repos.allocations.list_for_run(run_id):
        txn = repos.transactions.get(allocation.bank_transaction_id)
        expected = lookup(truth, txn)
        if expected is None:
            continue

        # A split has no single expected account, so it is scored separately
        # as "was it correctly identified as needing one".
        if expected.expected_split:
            rows.append({
                "description": txn.raw_description,
                "kind": "split",
                "correct": allocation.decision_method == DecisionMethod.HUMAN,
                "status": allocation.status,
                "confidence": allocation.confidence,
                "difficulty": expected.difficulty,
            })
            continue

        rows.append({
            "description": txn.raw_description,
            "kind": "single",
            "expected": expected.expected_account,
            "actual": allocation.account_id,
            "correct": allocation.account_id == expected.expected_account,
            "expected_tax": expected.expected_tax,
            "actual_tax": allocation.tax_code,
            "tax_correct": allocation.tax_code == expected.expected_tax,
            "status": allocation.status,
            "confidence": allocation.confidence,
            "decision_method": allocation.decision_method,
            "difficulty": expected.difficulty,
        })

    single = [r for r in rows if r["kind"] == "single"]
    graded = len(single) or 1
    auto = [r for r in single if r["status"] == AllocationStatus.AUTO_POSTED]
    queried = [r for r in single if r["status"] == AllocationStatus.CLIENT_QUERY]

    # Calibration: within each confidence band, what fraction was right.
    calibration: dict[str, dict] = defaultdict(lambda: {"n": 0, "correct": 0})
    for row in single:
        # Human answers are excluded - they are not predictions, and counting
        # them would flatter every band they land in.
        if row["confidence"] is None or row["decision_method"] == DecisionMethod.HUMAN:
            continue
        entry = calibration[band_for(row["confidence"])]
        entry["n"] += 1
        entry["correct"] += int(row["correct"])

    return {
        "graded": len(single),
        "accuracy": sum(r["correct"] for r in single) / graded,
        "tax_accuracy": sum(r["tax_correct"] for r in single) / graded,
        "auto_posted": len(auto),
        "auto_post_rate": len(auto) / graded,
        "auto_post_precision": (
            sum(r["correct"] for r in auto) / len(auto) if auto else None
        ),
        "wrongly_auto_posted": [r for r in auto if not r["correct"]],
        "client_queries": len(queried),
        # A transaction with no determinable answer that the system asked
        # about is a success, not a failure.
        "correctly_queried": sum(1 for r in queried if r["expected"] is None),
        "splits_identified": sum(
            1 for r in rows if r["kind"] == "split" and r["correct"]
        ),
        "calibration": dict(calibration),
        "misses": [r for r in single if not r["correct"]],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", default=CLIENT_UEN, help="client UEN")
    parser.add_argument("--live", action="store_true", help="use the real model")
    args = parser.parse_args()

    settings = get_settings()
    settings.use_stub_llm = not args.live

    from demo_learning_loop import files_for, resolve_client, truth_index

    from aiacct.db import DatabaseUnavailable, Repositories, check_connection
    from aiacct.graph import run_pipeline
    from aiacct.review import ReviewService
    from aiacct.testdata import load_for_client

    try:
        check_connection(settings)
    except DatabaseUnavailable as exc:
        print("\n" + str(exc) + "\n", file=sys.stderr)
        return 1

    repos = Repositories.open()
    client = resolve_client(repos, args.client)
    user = repos.users.get_or_create("Wei Ling Tan", "weiling@firm.example")

    period = sorted(load_for_client(args.client), key=lambda p: p.period)[0]
    truth = truth_index(period)

    mode = "live model" if args.live else "offline provider"
    print("")
    print(f"Evaluation - {client.name}, {period.period} ({mode})")
    print("First run only: no corrections have been made, so this is the")
    print("system with no memory of this client at all.")
    print("")

    state = run_pipeline(repos, client.id, files_for(period), user.id)
    if state.get("gate1_required"):
        service = ReviewService(repos)
        for issue in state.get("extraction_issues", []):
            if issue.get("field") and issue.get("document_id"):
                service.correct_extraction(
                    user.id, issue["field"], period.account_number,
                    document_id=issue["document_id"],
                )
        state = run_pipeline(repos, client.id, files_for(period), user.id)

    result = evaluate(repos, state["run_id"], truth)

    print(f"  transactions graded         {result['graded']}")
    print(f"  account accuracy            {result['accuracy']:.0%}")
    print(f"  tax code accuracy           {result['tax_accuracy']:.0%}")
    print(
        f"  auto-posted                 {result['auto_posted']} "
        f"({result['auto_post_rate']:.0%})"
    )

    precision = result["auto_post_precision"]
    shown = f"{precision:.0%}" if precision is not None else "n/a"
    print(
        f"  auto-post precision         {shown}"
        f"   <- of what nobody checked, how much was right"
    )
    print(
        f"  raised as client queries    {result['client_queries']}, of which "
        f"{result['correctly_queried']} were genuinely unanswerable"
    )

    print("")
    print("  Calibration - does the score mean anything?")
    print(f"    {'band':<14}{'n':>5}{'correct':>10}{'actual':>10}")
    for _, _, label in BANDS:
        entry = result["calibration"].get(label)
        if not entry or not entry["n"]:
            continue
        rate = entry["correct"] / entry["n"]
        print(f"    {label:<14}{entry['n']:>5}{entry['correct']:>10}{rate:>9.0%}")

    print("")
    if result["wrongly_auto_posted"]:
        print("  Auto-posted but wrong - these are the expensive failures:")
        for row in result["wrongly_auto_posted"]:
            print(
                f"    {row['description'][:44]:<46} "
                f"expected {row['expected']}, got {row['actual']}"
            )
    else:
        print("  Nothing was auto-posted incorrectly.")

    if result["misses"]:
        print("")
        print(
            f"  Flagged for review and still wrong ({len(result['misses'])}) - "
            f"these cost an accountant time but not correctness:"
        )
        for row in result["misses"][:6]:
            print(
                f"    {row['description'][:42]:<44} "
                f"want {str(row['expected']):<5} got {str(row['actual']):<5} "
                f"[{row['status']}]"
            )
            print(f"      why it is hard: {row['difficulty'][:86]}")

    repos.close()
    print("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
