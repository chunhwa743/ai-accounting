"""Reads the test data written down in ``data/testdata/*.md``.

The transactions are kept in markdown rather than Python so that someone who
knows accounting but not programming can read them, check them, and add cases.
The files are the source; the PDFs, scans, images and CSVs are rendered from
them and are not committed.

The `Account`, `Tax` and `Why this is hard` columns are the answer key. They sit
beside each transaction so the reasoning is visible where the case is, and this
parser strips them out of everything it renders - the pipeline only ever sees
generated files, and the markdown reaches nothing but the generator and the
evaluation harness.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from .models import SupportingDoc, Txn


@dataclass
class Period:
    """One statement and its supporting documents."""

    key: str                 # lumina-2026-01
    client_uen: str
    client_name: str
    period: str              # 2026-01
    bank: str
    account_number: str
    period_start: date
    period_end: date
    opening_balance: Decimal
    render_as: str           # pdf | scan | csv
    print_balances: bool
    # A smudged identifier on a scan. Fields listed here are what stop a run at
    # the extraction gate.
    unclear_header: dict[str, str] = field(default_factory=dict)
    transactions: list[Txn] = field(default_factory=list)


class TestDataError(ValueError):
    """Raised with the file and line, so a typo is findable."""


_HEADING = re.compile(r"^#\s+(.*?)\s+—\s+(.*)$")
_FIELD = re.compile(r"^-\s+([A-Za-z ]+):\s*(.*)$")
_DOC_HEADING = re.compile(r"^###\s+(\S+)\s+—\s+(.*)$")
_PERIOD = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})$")
_SPLIT = re.compile(r"^split:(.+)$")


def _decimal(value: str | None) -> Decimal | None:
    value = (value or "").strip()
    return Decimal(value) if value else None


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_divider(line: str) -> bool:
    return bool(re.fullmatch(r"\|[\s|:-]+\|", line.strip()))


def parse_file(path: Path) -> Period:
    lines = path.read_text(encoding="utf-8").splitlines()

    heading = next((line for line in lines if line.startswith("# ")), None)
    match = _HEADING.match(heading or "")
    if match is None:
        raise TestDataError(f"{path.name}: first line must be '# <Client> — <Month Year>'")
    client_name = match.group(1)

    meta: dict[str, str] = {}
    for line in lines:
        if line.startswith("## "):
            break
        field_match = _FIELD.match(line)
        if field_match:
            meta[field_match.group(1).strip().lower()] = field_match.group(2).strip()

    required = {"client", "bank", "account", "period", "opening balance", "render as"}
    missing = required - set(meta)
    if missing:
        raise TestDataError(f"{path.name}: missing header field(s) {sorted(missing)}")

    period_match = _PERIOD.match(meta["period"])
    if period_match is None:
        raise TestDataError(f"{path.name}: Period must be 'YYYY-MM-DD to YYYY-MM-DD'")
    start = date.fromisoformat(period_match.group(1))
    end = date.fromisoformat(period_match.group(2))

    unclear: dict[str, str] = {}
    if "unclear header" in meta:
        for item in meta["unclear header"].split(","):
            name, _, level = item.partition("=")
            unclear[name.strip()] = level.strip()

    documents = _parse_documents(path, lines)
    transactions = _parse_transactions(path, lines, documents)

    unattached = set(documents) - {t.description for t in transactions}
    if unattached:
        raise TestDataError(
            f"{path.name}: 'Settles:' names transaction(s) that do not exist: "
            f"{sorted(unattached)}"
        )

    return Period(
        key=path.stem,
        client_uen=meta["client"],
        client_name=client_name,
        period=f"{start:%Y-%m}",
        bank=meta["bank"],
        account_number=meta["account"],
        period_start=start,
        period_end=end,
        opening_balance=Decimal(meta["opening balance"]),
        render_as=meta["render as"].lower(),
        print_balances=meta.get("print balances", "yes").lower() != "no",
        unclear_header=unclear,
        transactions=transactions,
    )


def _parse_transactions(
    path: Path, lines: list[str], documents: dict[str, SupportingDoc]
) -> list[Txn]:
    try:
        start = next(i for i, l in enumerate(lines) if l.strip().lower() == "## transactions")
    except StopIteration:
        raise TestDataError(f"{path.name}: no '## Transactions' section") from None

    transactions: list[Txn] = []
    for offset, line in enumerate(lines[start + 1:], start=start + 2):
        stripped = line.strip()
        if stripped.startswith("## "):
            break
        if not stripped.startswith("|") or _is_divider(stripped):
            continue

        cells = _cells(stripped)
        if cells[0].lower() == "day":
            continue
        if len(cells) < 8:
            raise TestDataError(
                f"{path.name}:{offset}: expected 8 columns, found {len(cells)}"
            )

        day, description, reference, out, inn, account, tax, difficulty = cells[:8]

        expected_account: str | None = None
        expected_split = None
        if account.lower() == "none":
            # Genuinely unresolvable from the statement alone.
            pass
        elif (split_match := _SPLIT.match(account)):
            # One payment covering more than one account, e.g. a loan repayment
            # splitting into principal and interest.
            parts = []
            for piece in split_match.group(1).split(","):
                code, _, amount = piece.partition("=")
                parts.append((code.strip(), amount.strip()))
            expected_split = tuple(parts)
        else:
            expected_account = account

        transactions.append(
            Txn(
                day=int(day),
                description=description,
                reference=reference or None,
                money_out=_decimal(out),
                money_in=_decimal(inn),
                expected_account=expected_account,
                expected_tax=tax or None,
                expected_split=expected_split,
                difficulty=difficulty,
                doc=documents.get(description),
            )
        )

    if not transactions:
        raise TestDataError(f"{path.name}: no transactions found")
    return transactions


def _parse_documents(path: Path, lines: list[str]) -> dict[str, SupportingDoc]:
    """Returns documents keyed by the description of the transaction they settle."""
    try:
        start = next(
            i for i, l in enumerate(lines) if l.strip().lower() == "## supporting documents"
        )
    except StopIteration:
        return {}

    documents: dict[str, SupportingDoc] = {}
    number = vendor = None
    fields: dict[str, str] = {}
    items: list[str] = []
    in_items = False

    def flush() -> None:
        if number is None:
            return
        settles = fields.get("settles")
        if not settles:
            raise TestDataError(
                f"{path.name}: document {number} has no 'Settles:' line, so nothing "
                f"links it to a transaction"
            )
        documents[settles] = SupportingDoc(
            kind=fields.get("kind", "invoice").upper(),
            fmt=fields.get("render as", "pdf"),
            doc_number=number,
            doc_date=date.fromisoformat(fields["date"]),
            vendor_name=vendor,
            total=Decimal(fields["total"]),
            tax=Decimal(fields.get("tax", "0")),
            summary=fields.get("summary", ""),
            line_items=list(items),
        )

    for line in lines[start + 1:]:
        stripped = line.strip()
        heading = _DOC_HEADING.match(stripped)
        if heading:
            flush()
            number, vendor = heading.group(1), heading.group(2)
            fields, items, in_items = {}, [], False
            continue
        if stripped.startswith("- Items:"):
            in_items = True
            continue
        if in_items and stripped.startswith("- "):
            items.append(stripped[2:].strip())
            continue
        field_match = _FIELD.match(stripped)
        if field_match:
            in_items = False
            fields[field_match.group(1).strip().lower()] = field_match.group(2).strip()

    flush()
    return documents


def load_all(directory: Path | None = None) -> list[Period]:
    """Every period, sorted so a client's months run in order."""
    from ..config import PROJECT_ROOT

    directory = directory or PROJECT_ROOT / "data" / "testdata"
    # A directory of periods may also hold prose for the reader. Anything
    # starting with "_" or named README is documentation, not a statement.
    files = [
        path for path in sorted(directory.glob("*.md"))
        if not path.name.startswith("_") and path.stem.lower() != "readme"
    ]
    periods = [parse_file(path) for path in files]
    if not periods:
        raise TestDataError(f"no test data found in {directory}")
    return periods


def load_for_client(uen: str, directory: Path | None = None) -> list[Period]:
    return [p for p in load_all(directory) if p.client_uen == uen]
