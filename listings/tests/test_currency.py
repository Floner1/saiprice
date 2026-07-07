from decimal import Decimal

from django.test import SimpleTestCase

from listings.scraping.currency import parse_vnd


class ParseVndTests(SimpleTestCase):
    def test_ty_unit(self):
        self.assertEqual(parse_vnd("8 tỷ"), Decimal("8000000000"))

    def test_trieu_unit_with_decimal_comma(self):
        self.assertEqual(parse_vnd("~129,03 triệu/m²"), Decimal("129030000"))

    def test_negotiable_returns_none(self):
        self.assertIsNone(parse_vnd("Thỏa thuận"))
        self.assertIsNone(parse_vnd("Giá thỏa thuận"))

    def test_empty_returns_none(self):
        self.assertIsNone(parse_vnd(""))
        self.assertIsNone(parse_vnd(None))

    def test_no_recognizable_unit_returns_none(self):
        self.assertIsNone(parse_vnd("liên hệ"))
