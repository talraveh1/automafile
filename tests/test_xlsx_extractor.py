"""Tests for sectioned XLSX extraction."""

from __future__ import annotations

import pytest


def test_xlsx_sheets_become_sections(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    from automafile.extractors import xlsx as xlsx_ext

    path = tmp_path / "book.xlsx"
    wb = openpyxl.Workbook()
    wb.active.title = "Invoices"
    wb.active["A1"] = "invoice id"
    wb.active["B1"] = "amount"
    for name in ["Customers", "Archive"]:
        ws = wb.create_sheet(name)
        ws["A1"] = name.lower()
    wb.save(path)

    doc = xlsx_ext.extract(path)
    assert doc.total_sections == 3
    assert [section.label for section in doc.sections] == [
        "Sheet: Invoices",
        "Sheet: Customers",
        "Sheet: Archive",
    ]
    assert "invoice id" in doc.sections[0].text
    assert "customers" in doc.sections[1].text
