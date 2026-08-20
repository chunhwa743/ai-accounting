"""Deterministic offline provider.

Lets the whole pipeline, the test suite, and the synthetic-data steps run with
no API key and no network. It is a test double, not a fallback for production:
it replays a sidecar transcription for extraction and applies a small keyword
table for categorisation.

The keyword table deliberately gets several of the interesting cases *wrong* -
GRABFOOD, the laptop that should be capitalised, the loan repayment - so that
an offline run still exercises the review and correction paths rather than
producing a suspiciously clean result.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from ..config import Settings, get_settings
from ..models import (
    AccountCandidate,
    CategorisationBatch,
    ClassificationResult,
    ColumnMapping,
    DocumentType,
    MatchPatternProposal,
    StatementExtraction,
    SupportingDocExtraction,
    TransactionCategorisation,
)
from .client import LLMError, LLMResult

T = TypeVar("T", bound=BaseModel)

# description fragment -> (account code, top score)
KEYWORD_ACCOUNTS: list[tuple[str, str, float]] = [
    ("TELCOVA", "489", 0.93),
    ("NEXUSFIBRE", "489", 0.93),
    ("SP GROUP", "445", 0.92),
    ("SP SERVICES", "445", 0.92),
    ("IRAS-GST", "820", 0.90),
    ("IRAS", "830", 0.72),
    ("CPF BOARD", "825", 0.90),
    ("CPF SUBMISSION", "825", 0.90),
    ("SALARY", "477", 0.88),
    ("PAYROLL", "477", 0.88),
    ("RENT", "469", 0.90),
    ("GRABFOOD", "493", 0.58),   # wrong on purpose: this is staff welfare, not travel
    ("GRAB", "493", 0.86),
    ("COMFORTDELGRO", "493", 0.85),
    ("ADOBE", "463", 0.84),
    ("FIGMA", "463", 0.84),
    ("MICROSOFT", "463", 0.84),
    ("SERVICE CHARGE", "404", 0.90),
    ("ANNUAL FEE", "404", 0.88),
    ("FX FEE", "404", 0.82),
    ("INTEREST EARNED", "260", 0.88),
    ("CREDIT INTEREST", "260", 0.88),
    # These two fire on the invoice summary rather than the bank description,
    # which is the whole point of matching a document: "PAYNOW-ACME SUPPLIES"
    # says who was paid, and only the invoice says a laptop was bought.
    ("LAPTOP", "720", 0.91),
    ("DELL", "720", 0.90),
    ("STATIONERY", "461", 0.86),
    ("A4 PAPER", "461", 0.86),
    ("TONER", "461", 0.86),
    ("REPAINT", "473", 0.87),
    ("PARTITION", "473", 0.87),
    ("BROADBAND", "489", 0.90),
    ("ACME SUPPLIES", "453", 0.62),
    ("TECHPOINT", "453", 0.55),  # wrong on purpose: a laptop should be capitalised
    ("NTUC", "425", 0.61),
    ("FAIRPRICE", "425", 0.61),
    ("SHENG SIONG", "425", 0.60),
    ("INSURANCE", "433", 0.78),
    ("CLINIC", "483", 0.80),
    ("MEDICAL", "483", 0.80),
    ("SRC", "485", 0.70),
    ("CLUB", "485", 0.70),
    ("ESSO", "449", 0.80),
    ("SHELL", "449", 0.80),
    ("LOAN REPAYMENT", "437", 0.62),  # wrong on purpose: needs a principal/interest split
    ("LAZADA", "453", 0.65),
    ("SHOPEE", "453", 0.65),
    ("INVOICE", "200", 0.70),
    ("PAYMENT RECEIVED", "200", 0.78),
    ("TRANSFER", "090", 0.55),
]

TRANSACTION_BLOCK = re.compile(r"<transactions>(.*?)</transactions>", re.DOTALL)


class StubClient:
    """Mirrors :class:`OpenAIClient.parse` without a network call."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.calls = 0

    def parse(
        self,
        *,
        prompt: str,
        schema: type[T],
        files: list[Path] | None = None,
        images: list[Path] | None = None,
        effort: str = "medium",
        detail: str = "high",
        source_hint: Path | None = None,
    ) -> LLMResult[T]:
        self.calls += 1
        # source_hint carries the original file when its content was passed as
        # text, or when what was uploaded is a derived artefact such as a
        # rendered page image. It takes priority for exactly that reason.
        sources = [*(files or []), *(images or [])]
        if source_hint is not None:
            sources = [source_hint, *(s for s in sources if s != source_hint)]

        if schema is ClassificationResult:
            parsed = self._classify(sources)
        elif schema is StatementExtraction:
            parsed = self._replay(sources, StatementExtraction)
        elif schema is SupportingDocExtraction:
            parsed = self._replay(sources, SupportingDocExtraction)  # noqa: E501
        elif schema is CategorisationBatch:
            parsed = self._categorise(prompt)
        elif schema is MatchPatternProposal:
            parsed = self._match_pattern(prompt)
        elif schema is ColumnMapping:
            parsed = ColumnMapping(
                date_column=0, description_column=1, reference_column=2,
                debit_column=3, credit_column=4, balance_column=None,
                notes="stub provider assumed the common export layout",
            )
        else:
            raise LLMError(f"stub provider has no handler for {schema.__name__}")

        return LLMResult(parsed=parsed, model="stub", input_tokens=0, output_tokens=0)

    # ------------------------------------------------------------ handlers

    def _find_transcript(self, sources: list[Path]) -> Path | None:
        """Locate the transcript the generator wrote beside a source document.

        Uploaded files are copied into storage under a hashed name, so the
        sidecar is searched for by original filename under the replay root
        rather than assumed to sit next to the stored copy.
        """
        for source in sources:
            direct = source.with_suffix(source.suffix + ".extract.json")
            if direct.exists():
                return direct

        root = self.settings.stub_replay_dir
        if not root.exists():
            return None
        for source in sources:
            # Storage prefixes the original name with a short hash.
            name = source.name.split("-", 1)[-1] if "-" in source.name else source.name
            for candidate in (source.name, name):
                matches = list(root.rglob(f"{candidate}.extract.json"))
                if matches:
                    return matches[0]
        return None

    @staticmethod
    def _classify(sources: list[Path]) -> ClassificationResult:
        """Classify from the filename, which the generator makes descriptive."""
        name = sources[0].name.lower() if sources else ""
        if "statement" in name:
            kind = DocumentType.BANK_STATEMENT
        elif "invoice" in name:
            kind = DocumentType.INVOICE
        elif "receipt" in name:
            kind = DocumentType.RECEIPT
        elif "payroll" in name:
            kind = DocumentType.PAYROLL
        else:
            kind = DocumentType.OTHER
        return ClassificationResult(
            document_type=kind, reasoning=f"stub provider classified from filename {name!r}"
        )

    def _replay(self, sources: list[Path], schema: type[T]) -> T:
        """Load the transcription the generator wrote beside the document.

        This is a transcription of what is *printed*, not the categorisation
        answer key - the stub still has to decide the accounts itself.
        """
        if not sources:
            raise LLMError("stub extraction needs a source file")
        sidecar = self._find_transcript(sources)
        if sidecar is None:
            raise LLMError(
                f"no offline transcript found for {sources[0].name}; run "
                "scripts/generate_synthetic_data.py, or set OPENAI_API_KEY to "
                "extract for real"
            )
        return schema.model_validate_json(sidecar.read_text(encoding="utf-8"))

    def _categorise(self, prompt: str) -> CategorisationBatch:
        block = TRANSACTION_BLOCK.search(prompt)
        if not block:
            return CategorisationBatch(results=[])

        results = []
        for item in json.loads(block.group(1)):
            results.append(self._categorise_one(item))
        return CategorisationBatch(results=results)

    def _categorise_one(self, item: dict[str, Any]) -> TransactionCategorisation:
        description = (item.get("description") or "").upper()
        summary = (item.get("document_summary") or "").upper()
        haystack = f"{description} {summary}"

        matches = [
            (code, score)
            for fragment, code, score in KEYWORD_ACCOUNTS
            if fragment in haystack
        ]

        if not matches:
            # Nothing recognisable. Say so rather than inventing an account -
            # a null account_id routes this to a client query, which is the
            # honest outcome.
            return TransactionCategorisation(
                transaction_id=item["transaction_id"],
                account_code=None,
                tax_code=None,
                alternatives=[],
                reasoning="stub provider: no keyword matched this description",
                identifiable=bool(re.search(r"[A-Za-z]{3,}", description)),
                clarification_question=(
                    f"We could not determine what the payment described as "
                    f"{item.get('description')!r} was for. Could you confirm?"
                ),
                needs_split=False,
                split_note=None,
            )

        matches.sort(key=lambda m: -m[1])
        top_code, top_score = matches[0]
        alternatives = [AccountCandidate(account_code=c, score=s) for c, s in matches[:3]]
        if len(alternatives) == 1:
            # A lone candidate still needs a runner-up, otherwise the ambiguity
            # margin is meaningless.
            alternatives.append(AccountCandidate(account_code="429", score=max(0.0, top_score - 0.3)))

        return TransactionCategorisation(
            transaction_id=item["transaction_id"],
            account_code=top_code,
            tax_code=None,  # derived from the account downstream
            alternatives=alternatives,
            reasoning=f"stub provider matched a keyword to account {top_code}",
            identifiable=True,
            clarification_question=None,
            needs_split=False,
            split_note=None,
        )

    @staticmethod
    def _match_pattern(prompt: str) -> MatchPatternProposal:
        """Propose a rule pattern from the description in the prompt.

        Takes the leading alphabetic words after stripping payment-rail
        prefixes, which approximates what the real call returns.
        """
        match = re.search(r"<description>(.*?)</description>", prompt, re.DOTALL)
        text = (match.group(1) if match else prompt).strip().upper()
        for prefix in ("GIRO PAYMENT ", "PAYNOW-", "NETS QR PAYMENT ", "VISA ", "TRF "):
            if text.startswith(prefix):
                text = text[len(prefix):]
        tokens = re.findall(r"[A-Z][A-Z&*]*", text)[:2]
        return MatchPatternProposal(
            match_pattern=" ".join(tokens) if tokens else text[:20],
            reasoning="stub provider took the leading merchant tokens",
        )
