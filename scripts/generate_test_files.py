#!/usr/bin/env python
"""Render the test data into the files a client would actually send.

    python scripts/generate_test_files.py

Reads data/testdata/*.md and writes data/generated/. The markdown is the
source and is committed; these files are build output and are not.

Real clients send whatever they have - a clean PDF from one bank, a photo of a
receipt, a scan from a bad copier, a CSV export - so all of those come out of
one definition, which keeps the answer key in a single place while still
exercising every ingestion path.

Reproducible: the noise on the scan and the receipt photo comes from its own
seeded generator, and reportlab is put in invariant mode so its PDFs carry no
creation timestamp. The one exception is the scanned statement - Pillow stamps
a creation date into the PDF it writes and offers no way to turn that off - so
its bytes differ between runs even though its content does not.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from aiacct.testdata import Period, load_all  # noqa: E402
from aiacct.testdata.render import (  # noqa: E402
    degrade_to_scan,
    document_sidecar,
    render_invoice_pdf,
    render_payroll_docx,
    render_receipt_jpg,
    render_statement_csv,
    render_statement_pdf,
    statement_sidecar,
    write_sidecar,
)

OUT = ROOT / "data" / "generated"


def slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")


def render_period(period: Period, out_dir: Path) -> dict:
    statements = out_dir / "statements"
    documents = out_dir / "documents"
    statements.mkdir(parents=True, exist_ok=True)
    documents.mkdir(parents=True, exist_ok=True)

    stem = f"{slug(period.client_name)}-statement-{period.period}"

    if period.render_as == "pdf":
        target = statements / f"{stem}.pdf"
        render_statement_pdf(
            target, bank=period.bank, client_name=period.client_name,
            account_number=period.account_number, period=period.period,
            txns=period.transactions, opening=period.opening_balance,
        )

    elif period.render_as == "scan":
        # Rendered clean, then put through a copier: skew, blur, speckle and
        # JPEG artefacts, so the text layer is gone and the file has to go down
        # the vision path rather than being read directly.
        clean = statements / f".{stem}-clean.pdf"
        target = statements / f"{stem}-scan.pdf"
        render_statement_pdf(
            clean, bank=period.bank, client_name=period.client_name,
            account_number=period.account_number, period=period.period,
            txns=period.transactions, opening=period.opening_balance,
        )
        degrade_to_scan(clean, target)
        clean.unlink()

    elif period.render_as == "csv":
        target = statements / f"{stem}.csv"
        render_statement_csv(
            target, period=period.period, txns=period.transactions,
            opening=period.opening_balance, with_balances=period.print_balances,
        )
    else:
        raise ValueError(f"{period.key}: unknown 'Render as' value {period.render_as!r}")

    write_sidecar(target, statement_sidecar(
        bank=period.bank, account_number=period.account_number,
        account_holder=period.client_name, period=period.period,
        txns=period.transactions, opening=period.opening_balance,
        with_balances=period.print_balances, unclear_header=period.unclear_header,
    ))

    written = []
    for txn in period.transactions:
        if not txn.doc:
            continue
        doc = txn.doc
        name = f"{slug(doc.vendor_name)}-{doc.doc_number.lower()}"

        if doc.fmt == "pdf":
            path = documents / f"{name}-invoice.pdf"
            render_invoice_pdf(path, doc, buyer=period.client_name)
        elif doc.fmt == "jpg":
            path = documents / f"{name}-receipt.jpg"
            render_receipt_jpg(path, doc)
        elif doc.fmt == "docx":
            path = documents / f"{name}-payroll.docx"
            render_payroll_docx(path, doc)
        else:
            raise ValueError(f"{period.key}: unknown document format {doc.fmt!r}")

        write_sidecar(path, document_sidecar(doc))
        written.append(str(path.relative_to(OUT)))

    return {
        "statement_file": str(target.relative_to(OUT)),
        "format": period.render_as,
        "transaction_count": len(period.transactions),
        "supporting_documents": written,
        "balances_printed": period.print_balances,
        "unclear_header_fields": period.unclear_header,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", help="only this UEN")
    args = parser.parse_args()

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    periods = load_all()
    if args.client:
        periods = [p for p in periods if p.client_uen == args.client]

    print(f"\nRendering {len(periods)} period(s) from data/testdata/\n")

    manifest: dict[str, dict] = {}
    by_client: dict[str, list[Period]] = {}
    for period in periods:
        by_client.setdefault(period.client_name, []).append(period)

    for client_name, client_periods in by_client.items():
        print(f"  {client_name}")
        out_dir = OUT / slug(client_name)
        for period in sorted(client_periods, key=lambda p: p.period):
            detail = render_period(period, out_dir)
            manifest[period.key] = {"client_uen": period.client_uen, **detail}

            splits = sum(1 for t in period.transactions if t.expected_split)
            unresolvable = sum(
                1 for t in period.transactions
                if t.expected_account is None and not t.expected_split
            )
            print(
                f"    {period.period}  {detail['format']:<5} "
                f"{detail['transaction_count']:>3} transactions, "
                f"{len(detail['supporting_documents'])} documents, "
                f"{splits} split, {unresolvable} genuinely unresolvable"
            )
            if not period.print_balances:
                print("           no balances printed: reconciliation unverifiable")
            if period.unclear_header:
                fields = ", ".join(period.unclear_header)
                print(f"           unclear identifier: {fields} (stops at the extraction gate)")
        print()

    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Written to {OUT.relative_to(ROOT)} (not committed - this is build output)")
    print("The answer key stays in data/testdata/*.md and is read only by evaluate.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
