"""Getting file content into a usable shape, cheapest path first."""

from .readers import (
    map_columns,
    parse_amount,
    parse_date,
    read_docx_text,
    read_first_page_text,
    read_pdf_text,
    read_tabular_statement,
)
from .router import FileKind, RoutedFile, file_hash, route

__all__ = [
    "FileKind",
    "RoutedFile",
    "file_hash",
    "map_columns",
    "parse_amount",
    "parse_date",
    "read_docx_text",
    "read_first_page_text",
    "read_pdf_text",
    "read_tabular_statement",
    "route",
]
