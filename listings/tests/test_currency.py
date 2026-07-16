from decimal import Decimal

from django.test import SimpleTestCase

from listings.scraping.currency import parse_vnd, parse_vnd_unit


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

    def test_capitalized_unit_with_decimal_comma(self):
        self.assertEqual(parse_vnd("23,5 Tỷ"), Decimal("23500000000"))
        self.assertEqual(parse_vnd("3,75 Tỷ"), Decimal("3750000000"))
        self.assertEqual(parse_vnd("11500 Triệu"), Decimal("11500000000"))

    def test_tr_abbreviation_per_sqm(self):
        self.assertEqual(parse_vnd("46,2 tr/m2"), Decimal("46200000"))

    def test_price_range_returns_none(self):
        self.assertIsNone(parse_vnd("2 - 2,1 tỷ"))
        self.assertIsNone(parse_vnd("0 - 11500 Triệu"))

    def test_bare_number_without_unit_returns_none(self):
        self.assertIsNone(parse_vnd("1.200.000"))


class ParseVndUnitTests(SimpleTestCase):
    def test_ty_and_capitalized_ty(self):
        self.assertEqual(parse_vnd_unit("8 tỷ"), "tỷ")
        self.assertEqual(parse_vnd_unit("23,5 Tỷ"), "tỷ")

    def test_trieu_and_tr_normalize_to_trieu(self):
        self.assertEqual(parse_vnd_unit("~129,03 triệu/m²"), "triệu")
        self.assertEqual(parse_vnd_unit("46,2 tr/m2"), "triệu")

    def test_null_wherever_parse_vnd_is_null(self):
        self.assertIsNone(parse_vnd_unit("Thỏa thuận"))
        self.assertIsNone(parse_vnd_unit("2 - 2,1 tỷ"))
        self.assertIsNone(parse_vnd_unit("1.200.000"))
        self.assertIsNone(parse_vnd_unit(None))
