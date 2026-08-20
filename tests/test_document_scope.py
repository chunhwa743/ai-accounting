"""A run may only match the documents it was given.

Matching used to search every supporting document the client had ever
uploaded. A statement submitted on its own would then be resolved by invoices
from an earlier batch - evidence nobody supplied with it - and one invoice
could be claimed by several bank lines in different periods, which would make
a duplicate payment look fully documented when both halves pointed at the
same piece of paper.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from aiacct.db.models import Document, Run
from aiacct.models import DocumentType


def _run_with(repos, client_id: int, *, invoice: bool) -> int:
    run = repos.runs.create(Run(client_id=client_id))
    repos.documents.create(
        Document(
            client_id=client_id, document_type=DocumentType.BANK_STATEMENT,
            original_filename=f"statement-{run.id}.pdf",
            storage_uri=f"/tmp/s{run.id}.pdf", mime_type="application/pdf",
            file_hash=f"stmt-{run.id}",
        ),
        run_id=run.id,
    )
    if invoice:
        repos.documents.create(
            Document(
                client_id=client_id, document_type=DocumentType.INVOICE,
                original_filename=f"invoice-{run.id}.pdf",
                storage_uri=f"/tmp/i{run.id}.pdf", mime_type="application/pdf",
                file_hash=f"inv-{run.id}", vendor_name="Acme Supplies Pte Ltd",
                total_amount=Decimal("1962.00"), doc_date=date(2026, 5, 6),
            ),
            run_id=run.id,
        )
    return run.id


def test_a_run_sees_the_invoice_submitted_with_it(repos, agency):
    run_id = _run_with(repos, agency.id, invoice=True)
    docs = repos.documents.list_supporting_for_run(run_id)
    assert [d.document_type for d in docs] == [DocumentType.INVOICE]


def test_a_later_run_does_not_inherit_an_earlier_run_s_invoice(repos, agency):
    _run_with(repos, agency.id, invoice=True)
    statement_only = _run_with(repos, agency.id, invoice=False)
    assert repos.documents.list_supporting_for_run(statement_only) == [], (
        "a statement submitted alone was resolved using a previous batch's "
        "invoice, so the run was not reproducible from its own inputs"
    )


def test_the_statement_itself_is_not_offered_as_supporting_evidence(repos, agency):
    run_id = _run_with(repos, agency.id, invoice=True)
    kinds = {d.document_type for d in repos.documents.list_supporting_for_run(run_id)}
    assert DocumentType.BANK_STATEMENT not in kinds
