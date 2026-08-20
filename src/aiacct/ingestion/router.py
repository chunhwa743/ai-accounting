"""Works out what kind of file arrived and which reading path it needs.

Clients send whatever they have: a clean PDF from one bank, a phone photo of a
receipt, a scan from a bad copier, a CSV export, occasionally a Word document.
The cheapest viable path is chosen first, and a model is only involved when
there is genuinely no other way to read the content.
"""

from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..config import get_settings


class FileKind(StrEnum):
    PDF_DIGITAL = "PDF_DIGITAL"    # has a text layer; read it directly
    PDF_SCANNED = "PDF_SCANNED"    # no text layer; needs the vision path
    IMAGE = "IMAGE"                # must use input_image, not input_file
    TABULAR = "TABULAR"            # CSV/XLSX; already structured
    DOCX = "DOCX"
    TEXT = "TEXT"
    UNSUPPORTED = "UNSUPPORTED"


# Magic bytes, checked before extensions - a file named .pdf is not necessarily
# a PDF, and clients rename things.
_SIGNATURES: list[tuple[bytes, str]] = [
    (b"%PDF", "pdf"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"PK\x03\x04", "zip"),  # docx and xlsx are both zip containers
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"RIFF", "webp"),
]

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
TABULAR_SUFFIXES = {".csv", ".tsv", ".xlsx", ".xls"}


@dataclass
class RoutedFile:
    path: Path
    kind: FileKind
    mime_type: str
    file_hash: str
    page_count: int | None = None
    chars_per_page: float | None = None
    note: str = ""

    @property
    def needs_vision(self) -> bool:
        return self.kind in (FileKind.PDF_SCANNED, FileKind.IMAGE)


def file_hash(path: Path) -> str:
    """SHA-256 of the bytes, so a re-uploaded file is not processed twice."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _signature(path: Path) -> str | None:
    with open(path, "rb") as handle:
        head = handle.read(16)
    for magic, name in _SIGNATURES:
        if head.startswith(magic):
            return name
    return None


def _pdf_text_density(path: Path) -> tuple[int, float]:
    """Pages, and average extractable characters per page.

    This is the whole digital-versus-scanned decision, and it costs nothing.
    A statement rendered from a text layer yields thousands of characters; a
    photocopy yields zero.
    """
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        pages = len(pdf.pages)
        chars = sum(len(page.extract_text() or "") for page in pdf.pages)
    return pages, (chars / pages if pages else 0.0)


def route(path: Path) -> RoutedFile:
    settings = get_settings()
    suffix = path.suffix.lower()
    signature = _signature(path)
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    digest = file_hash(path)

    if signature == "pdf" or suffix == ".pdf":
        pages, density = _pdf_text_density(path)
        digital = density >= settings.digital_pdf_chars_per_page
        return RoutedFile(
            path=path,
            kind=FileKind.PDF_DIGITAL if digital else FileKind.PDF_SCANNED,
            mime_type="application/pdf",
            file_hash=digest,
            page_count=pages,
            chars_per_page=density,
            note=(
                f"{density:.0f} chars/page: "
                + ("text layer present, reading directly" if digital
                   else "no usable text layer, using the vision path")
            ),
        )

    if signature in {"png", "jpg", "gif", "webp"} or suffix in IMAGE_SUFFIXES:
        return RoutedFile(
            path=path, kind=FileKind.IMAGE, mime_type=mime, file_hash=digest,
            page_count=1,
            # Images are not a valid input_file type and have to be sent as
            # input_image; routing them wrongly produces an API error.
            note="image: sent as input_image",
        )

    if suffix in TABULAR_SUFFIXES:
        return RoutedFile(
            path=path, kind=FileKind.TABULAR, mime_type=mime, file_hash=digest,
            note="already structured: parsed directly, no model needed",
        )

    if suffix == ".docx":
        return RoutedFile(
            path=path, kind=FileKind.DOCX, mime_type=mime, file_hash=digest,
            # The API does not extract images embedded in non-PDF files, so
            # any embedded scan has to be pulled out and sent separately.
            note="docx: text and tables read locally",
        )

    if suffix in {".txt", ".md"}:
        return RoutedFile(path=path, kind=FileKind.TEXT, mime_type="text/plain", file_hash=digest)

    return RoutedFile(
        path=path, kind=FileKind.UNSUPPORTED, mime_type=mime, file_hash=digest,
        note=f"unsupported file type: {suffix or signature or 'unknown'}",
    )
