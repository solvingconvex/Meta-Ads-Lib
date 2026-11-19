import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.etree import ElementTree
from zipfile import ZipFile

from ad_library import Ad, Result, Search
from ad_report import write_report
from meta_ads import group_results


def record(library_id: str) -> dict:
    return {
        "ad_archive_id": library_id,
        "page_id": "123",
        "page_name": "Example",
        "start_date": "2026-05-20",
        "publisher_platform": ["FACEBOOK", "INSTAGRAM"],
        "snapshot": {"body": {"text": "{=1+1}"}},
    }


def connection(ids: list[str], more: bool) -> dict:
    return {
        "edges": [{"node": {"collated_results": [record(i) for i in ids]}}],
        "page_info": {"has_next_page": more},
    }


class SearchTests(unittest.TestCase):
    def test_limit_counts_unique_ids_across_pages(self):
        result = Result(Search("GB", "Page", "123"))
        self.assertFalse(result.consume(connection(["10", "10"], True), limit=2))
        self.assertTrue(result.consume(connection(["10", "11", "12"], False), limit=2))
        self.assertEqual(list(result.ads), ["10", "11"])
        self.assertTrue(result.capped)

    def test_overlapping_searches_keep_each_country_and_match(self):
        searches = [
            Search("GB", "Page", "123"),
            Search("GB", "Keyword", "tea"),
            Search("US", "Keyword", "tea"),
        ]
        results = [Result(search) for search in searches]
        for result in results:
            result.consume(connection(["10"], False), limit=0)
        ads, notes = group_results(["GB", "US"], results, limit=0)
        self.assertEqual(ads["GB"]["10"].matches, ["Page 123", "Keyword tea"])
        self.assertEqual(ads["US"]["10"].matches, ["Keyword tea"])
        self.assertEqual(notes, {"GB": [], "US": []})


class ExcelTests(unittest.TestCase):
    def test_literal_copy_exact_id_and_date(self):
        ad = Ad.from_record(record("9876543210987654321"))
        with TemporaryDirectory() as folder:
            path = Path(folder) / "ads.xlsx"
            write_report(path, {"GB": {ad.library_id: ad}}, {}, {"GB": []}, datetime.now(UTC))
            with ZipFile(path) as archive:
                sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        cells = {c.get("r"): c for c in sheet.findall(".//{*}c")}
        self.assertEqual("".join(cells["A4"].itertext()), ad.library_id)
        self.assertEqual(cells["B4"].findtext("{*}v"), "46162")
        self.assertEqual("".join(cells["C4"].itertext()), "Facebook, Instagram")
        self.assertEqual("".join(cells["F4"].itertext()), "{=1+1}")
        self.assertIsNone(sheet.find(".//{*}f"))
        self.assertIsNotNone(sheet.find(".//{*}hyperlink[@ref='A4']"))

    def test_oversize_cell_preserves_previous_report(self):
        raw = record("1234567890")
        raw["snapshot"]["body"]["text"] = "x" * 32_768
        ad = Ad.from_record(raw)
        with TemporaryDirectory() as folder:
            path = Path(folder) / "ads.xlsx"
            path.write_bytes(b"previous export")
            with self.assertRaisesRegex(ValueError, "cell limit"):
                write_report(path, {"GB": {ad.library_id: ad}}, {}, {"GB": []}, datetime.now(UTC))
            self.assertEqual(path.read_bytes(), b"previous export")
            self.assertEqual(list(Path(folder).iterdir()), [path])
