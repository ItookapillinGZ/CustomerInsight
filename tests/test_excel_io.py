import io
import unittest
from unittest.mock import patch
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from core.config import Config
from core.excel_io import (
    ExcelInputError, inspect_workbook, prepare_excel_feedback_rows, read_feedback_xlsx,
)
from models.enums import ProcessingStatus


def make_xlsx(sheets):
    """Small OOXML fixture, built without requiring an Excel writer library."""
    stream = io.BytesIO()
    spreadsheet = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    relationships = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, len(sheets) + 1)) + "</Types>")
        archive.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr("xl/workbook.xml", f'<workbook xmlns="{spreadsheet}" xmlns:r="{relationships}"><sheets>' + "".join(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>' for i, (name, _) in enumerate(sheets, 1)) + "</sheets></workbook>")
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + "".join(f'<Relationship Id="rId{i}" Type="{relationships}/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(sheets) + 1)) + "</Relationships>")
        for sheet_number, (_, rows) in enumerate(sheets, 1):
            row_xml = []
            for row_number, row in enumerate(rows, 1):
                cells = []
                for column_number, value in enumerate(row):
                    coordinate = f"{chr(65 + column_number)}{row_number}"
                    if value is None:
                        continue
                    if isinstance(value, tuple) and value[0] == "formula":
                        cells.append(f'<c r="{coordinate}"><f>{escape(value[1])}</f><v>0</v></c>')
                    elif isinstance(value, (int, float)):
                        cells.append(f'<c r="{coordinate}" t="n"><v>{value}</v></c>')
                    else:
                        cells.append(f'<c r="{coordinate}" t="inlineStr"><is><t>{escape(value)}</t></is></c>')
                row_xml.append(f'<row r="{row_number}">' + "".join(cells) + "</row>")
            columns = max((len(row) for row in rows), default=1)
            archive.writestr(f"xl/worksheets/sheet{sheet_number}.xml", f'<worksheet xmlns="{spreadsheet}"><dimension ref="A1:{chr(64 + columns)}{max(1, len(rows))}"/><sheetData>' + "".join(row_xml) + "</sheetData></worksheet>")
    return stream.getvalue()


def replace_part(data, part, content):
    stream = io.BytesIO()
    with ZipFile(io.BytesIO(data)) as source, ZipFile(stream, "w", ZIP_DEFLATED) as target:
        for name in source.namelist():
            target.writestr(name, content if name == part else source.read(name))
    return stream.getvalue()


class ExcelInputTests(unittest.TestCase):
    def test_sheet_selection_preserves_original_columns_and_excel_rows(self):
        data = make_xlsx([
            ("说明", [["提示"], ["另一个工作表"]]),
            ("评价", [["产品", "评价"], ["A", " 好用 "], [None, None], ["B", "发货慢"]]),
        ])
        self.assertEqual(inspect_workbook(data, "评价.xlsx"), ["说明", "评价"])
        frame = read_feedback_xlsx(data, "评价.xlsx", "评价")
        rows = prepare_excel_feedback_rows(frame, "评价")
        self.assertEqual(frame.columns.tolist(), ["产品", "评价"])
        self.assertEqual(frame.loc[0, "评价"], " 好用 ")
        self.assertEqual([row.source_row for row in rows], [2, 3, 4])
        self.assertEqual(rows[1].processing_status, ProcessingStatus.SKIPPED_EMPTY)
        self.assertEqual(rows[2].original_text, "发货慢")
        self.assertEqual(frame.attrs["sheet_name"], "评价")

    def test_formula_review_is_rejected_but_other_formula_columns_do_not_block_text(self):
        data = make_xlsx([("评价", [
            ["评价", "金额"], [("formula", 'HYPERLINK("https://example.invalid","好评")'), 30],
            ["=SUM(A1)", 20], ["很好用", ("formula", "2+2")], [" 很好用 ", 10], [123, 10],
        ])])
        frame = read_feedback_xlsx(data, "reviews.xlsx")
        rows = prepare_excel_feedback_rows(frame, "评价")
        self.assertEqual([row.processing_status for row in rows], [
            ProcessingStatus.SKIPPED_INVALID, ProcessingStatus.PENDING, ProcessingStatus.PENDING,
            ProcessingStatus.SKIPPED_DUPLICATE, ProcessingStatus.SKIPPED_INVALID,
        ])
        self.assertTrue(rows[0].original_text.startswith("=HYPERLINK"))
        self.assertEqual(rows[3].duplicate_of, "row-4")
        self.assertIn("公式", rows[0].error_message)

    def test_formula_header_empty_duplicate_and_numeric_headers_are_rejected(self):
        for header in [["评价", " 评价 "], ["评价", None], [123], [("formula", "1+1")]]:
            with self.subTest(header=header):
                with self.assertRaises(ExcelInputError):
                    read_feedback_xlsx(make_xlsx([("S", [header, ["好用"]])]), "reviews.xlsx")

    def test_old_format_corrupt_archive_empty_and_invalid_extension_are_rejected(self):
        good = make_xlsx([("S", [["评价"], ["好用"]])])
        for data, filename in [(good, "old.xls"), (good, "a.csv"), (b"bad", "a.xlsx"), (b"", "a.xlsx")]:
            with self.subTest(filename=filename, data=data[:5]):
                with self.assertRaises(ExcelInputError):
                    inspect_workbook(data, filename)
        with self.assertRaisesRegex(ExcelInputError, "另存为"):
            inspect_workbook(good, "old.xls")

    def test_upload_uncompressed_size_sheet_count_and_data_row_limits(self):
        good = make_xlsx([("S", [["评价"], ["好用"]])])
        with patch.object(Config, "MAX_CSV_BYTES", 10):
            with self.assertRaises(ExcelInputError):
                inspect_workbook(good, "reviews.xlsx")
        with patch("core.excel_io.MAX_UNCOMPRESSED_BYTES", 10):
            with self.assertRaisesRegex(ExcelInputError, "解压"):
                inspect_workbook(good, "reviews.xlsx")
        with self.assertRaisesRegex(ExcelInputError, "工作表"):
            inspect_workbook(make_xlsx([(f"S{i}", [["评价"], ["好用"]]) for i in range(21)]))
        with patch.object(Config, "MAX_BATCH_ROWS", 2):
            with self.assertRaises(ExcelInputError):
                inspect_workbook(make_xlsx([("S", [["评价"], ["a"], ["b"], ["c"]])]))

    def test_forged_dimensions_cannot_hide_distant_cells_and_oversized_columns(self):
        good = make_xlsx([("S", [["评价"], ["好用"]])])
        with ZipFile(io.BytesIO(good)) as archive:
            xml = archive.read("xl/worksheets/sheet1.xml").decode()
        for altered in [xml.replace('r="A2"', 'r="A50000"'), xml.replace("A1:A2", "A1:XFD2"), xml.replace("A1:A2", "A1:A1")]:
            with self.subTest(altered=altered):
                with self.assertRaises(ExcelInputError):
                    inspect_workbook(replace_part(good, "xl/worksheets/sheet1.xml", altered))

    def test_entity_declarations_and_external_links_are_rejected_before_read(self):
        good = make_xlsx([("S", [["评价"], ["好用"]])])
        bad = replace_part(good, "xl/workbook.xml", '<!DOCTYPE workbook [<!ENTITY x "oops">]><workbook/>')
        with self.assertRaises(ExcelInputError):
            inspect_workbook(bad)
        stream = io.BytesIO(good)
        with ZipFile(stream, "a") as archive:
            archive.writestr("xl/externalLinks/externalLink1.xml", "<externalLink/>")
        with self.assertRaisesRegex(ExcelInputError, "外部"):
            inspect_workbook(stream.getvalue())

    def test_missing_sheet_and_header_only_are_actionable(self):
        good = make_xlsx([("S", [["评价"], ["好用"]])])
        with self.assertRaisesRegex(ExcelInputError, "不存在"):
            read_feedback_xlsx(good, "reviews.xlsx", "missing")
        with self.assertRaisesRegex(ExcelInputError, "只有表头"):
            read_feedback_xlsx(make_xlsx([("S", [["评价"]])]))


if __name__ == "__main__":
    unittest.main()
