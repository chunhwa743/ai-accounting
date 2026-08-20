#!/usr/bin/env python
"""Three months of one client, with a review in between each.

    python scripts/demo_learning_loop.py            # offline provider
    python scripts/demo_learning_loop.py --live     # the real model

Runs January, applies the corrections an accountant would actually make, then
runs February and March against the same client. The point is the table at the
end: the share of transactions resolved from learned rules should climb, the
number of model calls should fall, and the review queue should shrink - without
accuracy dropping.

Requires the database migrated and seeded, and the test files rendered:

    alembic upgrade head
    python scripts/seed_db.py
    python scripts/generate_test_files.py
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from aiacct.config import get_settings  # noqa: E402

GENERATED = ROOT / "data" / "generated"
CLIENT_UEN = "202512345A"  # Lumina Design Studio, seeded by scripts/seed_db.py


def resolve_client(repos, uen: str):
    """Find the seeded client. Scripts do not invent master data."""
    client = repos.clients.get_by_uen(uen)
    if client is None:
        raise SystemExit(
            f"No client with UEN {uen} in the database. Run: python scripts/seed_db.py"
        )
    return client


def files_for(period) -> list[str]:
    """The rendered files for one period, as a client would send them."""
    manifest_path = GENERATED / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit("Run: python scripts/generate_test_files.py")
    entry = json.loads(manifest_path.read_text(encoding="utf-8")).get(period.key)
    if entry is None:
        raise SystemExit(
            f"No generated files for {period.key}. "
            f"Run: python scripts/generate_test_files.py"
        )
    return [str(GENERATED / entry["statement_file"])] + [
        str(GENERATED / d) for d in entry["supporting_documents"]
    ]


def truth_key(day: int, money_out, money_in) -> tuple:
    """Identify a bank line by date and amount, not by its description text.

    A real statement often runs the reference number into the description
    column, and how a model splits them varies. Date plus amount identifies a
    line unambiguously - and where it does not, that is the duplicate case,
    which the system flags anyway.
    """
    amount = money_out if money_out is not None else money_in
    return (day, str(amount), money_in is not None)


def truth_index(period) -> dict:
    return {
        truth_key(t.day, t.money_out, t.money_in): t for t in period.transactions
    }


def lookup(truth: dict, txn):
    return truth.get(truth_key(txn.txn_date.day, txn.money_out, txn.money_in))


def review_run(repos, review, run_id: int, truth: dict, user_id: int) -> dict:
    """Play the accountant.

    Approves what is right, corrects what is wrong, and ticks "always do this"
    on the recurring merchants - which is what a real reviewer does, and what
    makes the next month cheaper.
    """
    from aiacct.models import AllocationStatus

    stats = {"approved": 0, "corrected": 0, "rules": 0, "split": 0, "queried": 0}

    for allocation in repos.allocations.list_for_run(run_id):
        txn = repos.transactions.get(allocation.bank_transaction_id)
        expected = lookup(truth, txn)
        if expected is None:
            continue

        if expected.expected_split:
            parts = [(code, Decimal(amount)) for code, amount in expected.expected_split]
            review.split(allocation.id, user_id, parts, note="per the loan schedule")
            stats["split"] += 1
            continue

        if expected.expected_account is None:
            # Genuinely unknowable from the statement. The accountant asks the
            # client rather than inventing an answer.
            stats["queried"] += 1
            continue

        if allocation.account_id == expected.expected_account:
            # Right answer. If it was flagged rather than auto-posted, the
            # reviewer also says "always do this", which is what stops the same
            # merchant being queued for review every month.
            teach = allocation.status != AllocationStatus.AUTO_POSTED
            outcome = review.approve(allocation.id, user_id, create_rule=teach)
            stats["approved"] += 1
            if outcome.rule is not None:
                stats["rules"] += 1
            continue

        recurring = (
            "repeat" in expected.difficulty
            or expected.difficulty.startswith("straightforward")
        )
        outcome = review.override(
            allocation.id, user_id, expected.expected_account,
            note="corrected during review",
            create_rule=recurring or allocation.status != AllocationStatus.AUTO_POSTED,
        )
        stats["corrected"] += 1
        if outcome.rule is not None:
            stats["rules"] += 1

    return stats


def score_run(repos, run_id: int, truth: dict) -> dict:
    """Compare against the answer key. Never read by the pipeline itself."""
    from aiacct.models import AllocationStatus, DecisionMethod

    total = correct = auto = 0
    for allocation in repos.allocations.list_for_run(run_id):
        txn = repos.transactions.get(allocation.bank_transaction_id)
        expected = lookup(truth, txn)
        if expected is None or expected.expected_split:
            continue
        total += 1
        correct += int(allocation.account_id == expected.expected_account)
        if allocation.status == AllocationStatus.AUTO_POSTED:
            auto += 1

    methods = repos.allocations.decision_method_counts(run_id)
    statuses = repos.allocations.status_counts(run_id)
    graded = total or 1

    return {
        "transactions": sum(statuses.values()),
        "accuracy": correct / graded,
        "auto_post_rate": auto / graded,
        "needs_attention": statuses.get("NEEDS_REVIEW", 0) + statuses.get("CLIENT_QUERY", 0),
        "from_rules": methods.get(str(DecisionMethod.RULE), 0),
        "from_model": methods.get(str(DecisionMethod.LLM), 0),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", default=CLIENT_UEN, help="client UEN")
    parser.add_argument("--live", action="store_true", help="use the real model")
    args = parser.parse_args()

    settings = get_settings()
    settings.use_stub_llm = not args.live

    from aiacct.db import DatabaseUnavailable, Repositories, check_connection
    from aiacct.export import export_all
    from aiacct.graph import run_pipeline
    from aiacct.llm import get_llm_client
    from aiacct.review import ReviewService
    from aiacct.testdata import load_for_client

    try:
        check_connection(settings)
    except DatabaseUnavailable as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1

    repos = Repositories.open()
    llm = get_llm_client(settings)
    review = ReviewService(repos, llm)

    client = resolve_client(repos, args.client)
    user = repos.users.get_or_create("Wei Ling Tan", "weiling@firm.example")
    periods = sorted(load_for_client(args.client), key=lambda p: p.period)
    if not periods:
        print(f"No test data for UEN {args.client}", file=sys.stderr)
        return 1

    mode = "live model" if args.live else "offline provider"
    print(f"\nAI Accounting Assistant - learning loop demo ({mode})")
    print(f"Client: {client.name}\n")

    results: list[dict] = []
    exported: dict = {}

    for period in periods:
        truth = truth_index(period)
        print(
            f"  {period.period}  ({period.render_as}, "
            f"{len(period.transactions)} transactions)"
        )
        rules_before = len(repos.rules.list_active(client.id))

        state = run_pipeline(repos, client.id, files_for(period), user.id)

        if state.get("gate1_required"):
            # Something could not be read. In the API this is where an
            # accountant sees one field and a crop of the page.
            print("      gate 1: extraction needs a person")
            for issue in state.get("extraction_issues", [])[:2]:
                print(f"        - {issue['message'][:86]}")
            for issue in state.get("extraction_issues", []):
                if issue.get("field") and issue.get("document_id"):
                    review.correct_extraction(
                        user.id, issue["field"], period.account_number,
                        document_id=issue["document_id"],
                    )
            print("      (supplied by hand, continuing)\n")
            state = run_pipeline(repos, client.id, files_for(period), user.id)

        scored = score_run(repos, state["run_id"], truth)
        scored["period"] = period.period
        scored["llm_calls"] = state["llm_calls"]

        reviewed = review_run(repos, review, state["run_id"], truth, user.id)
        repos.runs.complete(state["run_id"], user.id)
        scored["rules_after"] = len(repos.rules.list_active(client.id))
        results.append(scored)

        print(
            f"      {scored['from_rules']:>2} from learned rules, "
            f"{scored['from_model']:>2} from the model, "
            f"{state['llm_calls']} model call(s)"
        )
        print(
            f"      reviewed: {reviewed['approved']} approved, "
            f"{reviewed['corrected']} corrected, {reviewed['split']} split, "
            f"{reviewed['queried']} queried to the client"
        )
        print(f"      rules learned: {rules_before} -> {scored['rules_after']}\n")

        exported = export_all(repos, state["run_id"], settings.export_dir)

    print("=" * 78)
    print(
        f"{'period':<10}{'txns':>6}{'accuracy':>10}{'auto-post':>11}"
        f"{'from rules':>12}{'model calls':>13}{'to review':>11}"
    )
    print("-" * 78)
    for row in results:
        print(
            f"{row['period']:<10}{row['transactions']:>6}"
            f"{row['accuracy']:>9.0%}{row['auto_post_rate']:>11.0%}"
            f"{row['from_rules']:>12}{row['llm_calls']:>13}"
            f"{row['needs_attention']:>11}"
        )
    print("=" * 78)

    first, last = results[0], results[-1]
    print(
        f"\nAcross {len(results)} months: transactions resolved from learned rules "
        f"went from {first['from_rules']} to {last['from_rules']}, and model calls "
        f"from {first['llm_calls']} to {last['llm_calls']}."
    )
    print(
        "Rules are per client and applied deterministically, so the same "
        "description always produces the same answer."
    )
    print(f"\nExports written to {settings.export_dir}")
    for name, path in exported.items():
        print(f"  {name:<16} {path.name}")

    repos.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
