"""Call 1: what kind of document is this?

Only page one is sent. A statement announces itself in its header, and so does
an invoice, so there is no reason to pay to look at twelve pages to answer a
one-word question. On a scanned statement that is the difference between a few
hundred tokens and fifteen thousand.

Classification is split from extraction rather than combined into one call
because the two need different output schemas and different instructions, and
because a misclassification is then caught for almost nothing instead of
poisoning a full extraction.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..ingestion import FileKind, RoutedFile, read_first_page_text
from ..llm import LLMClient
from ..models import ClassificationResult, DocumentType

log = logging.getLogger(__name__)

PROMPT = """\
You are classifying a document that a client has sent to their accountant.

Decide which of these it is:

  BANK_STATEMENT  a statement of account from a bank, listing transactions with
                  running balances over a period
  INVOICE         a bill from a supplier, or one the client issued, showing an
                  invoice number and payment terms
  RECEIPT         proof that a payment was made, typically from a till
  PAYROLL         a payslip or payroll summary
  OTHER           anything else, including documents you cannot identify

Judge from the document's own headings and structure. If you are not confident,
answer OTHER rather than guessing - a wrong answer here sends the document to
the wrong extractor, whereas OTHER simply asks a human to look.
"""

TEXT_PROMPT = PROMPT + """
Here is the first page:

<page>
{page}
</page>
"""


def classify_document(
    routed: RoutedFile, llm: LLMClient, effort: str = "low"
) -> tuple[DocumentType, str, int, int]:
    """Return the type, the reason, and token usage.

    For anything with a readable text layer, only that text is sent. Images and
    scans send page one itself, because there is nothing else to send.
    """
    if routed.kind in (FileKind.PDF_SCANNED, FileKind.IMAGE):
        page_one = _first_page_image(routed)
        result = llm.parse(
            prompt=PROMPT,
            schema=ClassificationResult,
            images=[page_one] if page_one else None,
            files=[routed.path] if not page_one else None,
            effort=effort,
            # Classification does not need fine detail, only the shape of the
            # page and its headings.
            detail="low",
            source_hint=routed.path,
        )
    else:
        kind = {
            FileKind.PDF_DIGITAL: "pdf",
            FileKind.DOCX: "docx",
            FileKind.TABULAR: "tabular",
            FileKind.TEXT: "text",
        }.get(routed.kind, "text")
        text = read_first_page_text(routed.path, kind)
        result = llm.parse(
            prompt=TEXT_PROMPT.format(page=text),
            schema=ClassificationResult,
            files=[routed.path] if routed.kind == FileKind.TABULAR else None,
            effort=effort,
            source_hint=routed.path,
        )

    return (
        result.parsed.document_type,
        result.parsed.reasoning,
        result.input_tokens,
        result.output_tokens,
    )


def _first_page_image(routed: RoutedFile) -> Path | None:
    """Render page one of a scan so only that page is sent.

    Images are already a single page and go through unchanged.
    """
    if routed.kind == FileKind.IMAGE:
        return routed.path
    try:
        import pypdfium2
        from ..config import get_settings

        settings = get_settings()
        settings.ensure_dirs()
        target = settings.upload_dir / f".classify-{routed.file_hash[:12]}.png"
        if not target.exists():
            pdf = pypdfium2.PdfDocument(str(routed.path))
            pdf[0].render(scale=1.5).to_pil().save(target)
            pdf.close()
        return target
    except Exception as exc:  # noqa: BLE001
        # Falling back to the whole file costs tokens but still works.
        log.warning("could not render page 1 of %s (%s); sending whole file", routed.path.name, exc)
        return None
