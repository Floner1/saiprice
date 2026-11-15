import re
import unicodedata
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from django.test import SimpleTestCase

from listings.scraping.parsers import mask_phones
from listings.scraping.sites import homedy

FIXTURE = Path(__file__).resolve().parent.parent.parent / "testdata" / "homedy_srp_apartment.txt"
TODAY = date(2026, 11, 15)

# Every district label alonhadat stores (Peter's C0 listing, 2026-11-15).
ALONHADAT_LABELS = [
    "Thành phố Thủ Đức", "Quận 7", "Quận Tân Phú", "Quận 8", "Quận 5",
    "Quận Bình Thạnh", "Huyện Bình Chánh", "Quận Bình Tân", "Quận Tân Bình",
    "Quận 6", "Quận 1", "Quận 10", "Quận 12", "Quận 4", "Quận Gò Vấp",
    "Quận Phú Nhuận", "Quận 11", "Quận 3", "Huyện Nhà Bè", "Huyện Hóc Môn",
    "Huyện Cần Giờ",
]


def _card(href, title="Bán căn hộ 2PN", price="3 Tỷ", area="70 m2",
          address="Quận 7,  TP Hồ Chí Minh", time="2 ngày trước", data_id="1"):
    return f"""
<div class="product-item"><div class="product-item-top">
<div class="product-content">
<h3><a class="title padding-hoz" href="{href}" title="{title}">{title}</a></h3>
<ul class="product-unit">
<li><i class="icon icon-price"></i> <span class="price">{price}</span></li>
<li class="acr"><i class="icon icon-acr"></i> <span class="acreage">{area}</span></li>
<li>40 tr/m2</li>
<li class="address" title="Đường A, Phường B, {address}"><i class="icon icon-address"></i>  {address}</li>
</ul>
<div class="box-price-agency"><div class="time">{time}</div>
<a class="btn cmd-save" data-id="{data_id}" href="javascript:void(0)"></a></div>
</div></div></div>"""


def _parse(*cards):
    return homedy.parse_srp("".join(cards), "apartment", "sale", today=TODAY)


class MaskPhonesTests(SimpleTestCase):
    def test_masks_every_phone_form_and_keeps_the_rest(self):
        for text in (
            "Căn hộ 2PN LH 0901 234 567",
            "Căn hộ 2PN LH +84901234567",
            "Căn hộ 2PN LH +84 901 234 567",
            "Căn hộ 2PN LH (+84) 901.234.567",
            "Căn hộ 2PN LH 0901.234.567",
            "Căn hộ 2PN LH0901234567",
            "Căn hộ 2PN LH.0901234567",
            "Căn hộ 2PN LH,0901 234 567",
            "Căn hộ 2PN LH 0901 23 45 67",
            "Căn hộ 2PN LH 090 123 4567",
            "Căn hộ 2PN LH 028 3822 1234",
            "Căn hộ 2PN LH 0901-234-567",
        ):
            with self.subTest(text=text):
                masked = mask_phones(text)
                self.assertIn("2PN", masked)
                self.assertNotRegex(masked, r"\d{3}")

    def test_leaves_prices_areas_and_ids_alone(self):
        for text in (
            "Giá 2.050.000.000",
            "Giá 10.000.000.000",
            "Mã 1,0901234567",
            "Mã căn 090123456789",
            "Căn 84 m2, giá 1.840.000.000",
            "Chung cư, full NT, 9triệu, 2PN, 65m2",
            "Bán căn hộ 3PN, 2WC, 103m2, giá 28tỷ5",
        ):
            with self.subTest(text=text):
                self.assertEqual(mask_phones(text), text)

    def test_none_stays_none(self):
        self.assertIsNone(mask_phones(None))


class NormalizeDistrictTests(SimpleTestCase):
    def test_every_alonhadat_label_maps_to_itself(self):
        for label in ALONHADAT_LABELS:
            with self.subTest(label=label):
                self.assertEqual(homedy.normalize_district(label), label)

    def test_homedy_label_forms(self):
        cases = {
            "quận 1,  TP Hồ Chí Minh": "Quận 1",
            "Quận 5,  Thành phố Hồ Chí Minh": "Quận 5",
            "Quận Gò Vấp,  Thành phố Hồ Chí Minh.": "Quận Gò Vấp",
            "Quận Gò Vấp, Thành phố Hồ Chí Minh.": "Quận Gò Vấp",
            "Quận 2,  Thành phố Hồ Chí Minh": "Thành phố Thủ Đức",
            "Quận 2,  Hồ Chí Minh": "Thành phố Thủ Đức",
            "Quận 9,  TP Hồ Chí Minh": "Thành phố Thủ Đức",
            "Quận 9": "Thành phố Thủ Đức",
            "Quận Thủ Đức,  Thành phố Hồ Chí Minh": "Thành phố Thủ Đức",
            "Thủ Đức": "Thành phố Thủ Đức",
            "TP. Thủ Đức, TP. Hồ Chí Minh": "Thành phố Thủ Đức",
            "Bình Chánh,  TP. HCM": "Huyện Bình Chánh",
            "Huyện Bình Chánh,  TP Hồ Chí Minh": "Huyện Bình Chánh",
            "Quận Bình Chánh,  Thành phố Hồ Chí Minh": "Huyện Bình Chánh",
            "Huyện Nhà Bè,  TP Hồ Chí Minh": "Huyện Nhà Bè",
            "QUẬN TÂN BÌNH, TP Hồ Chí Minh": "Quận Tân Bình",
            "Quận 7.": "Quận 7",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(homedy.normalize_district(text), expected)

    def test_decomposed_unicode_still_matches(self):
        decomposed = unicodedata.normalize("NFD", "Quận Gò Vấp,  TP Hồ Chí Minh")
        self.assertEqual(homedy.normalize_district(decomposed), "Quận Gò Vấp")

    def test_outside_hcmc_or_empty_is_none(self):
        for text in ("Biên Hòa, Đồng Nai", "Thành phố Hồ Chí Minh", "", None):
            with self.subTest(text=text):
                self.assertIsNone(homedy.normalize_district(text))


class ParseRelativeDateTests(SimpleTestCase):
    def test_wordings_seen_on_the_live_pages(self):
        cases = {
            "2 ngày trước": TODAY - timedelta(days=2),
            "3 giờ trước": TODAY,
            "45 phút trước": TODAY,
            "2 tuần trước": TODAY - timedelta(days=14),
            "Hôm nay": TODAY,
            "Hôm qua": TODAY - timedelta(days=1),
            "\n3 ngày trước                                    ": TODAY - timedelta(days=3),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(homedy.parse_relative_date(text, TODAY), expected)

    def test_unknown_text_is_none(self):
        for text in ("Tháng trước", "2 tháng trước", "15/11/2026", "", None):
            with self.subTest(text=text):
                self.assertIsNone(homedy.parse_relative_date(text, TODAY))


class ParseSrpFixtureTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.listings, cls.skips = homedy.parse_srp(
            FIXTURE.read_text(encoding="utf-8"), "apartment", "sale", today=TODAY
        )
        cls.by_id = {p.source_id: p for p in cls.listings}

    def test_every_card_parses(self):
        self.assertEqual(len(self.listings), 5)
        self.assertEqual(self.skips, [])

    def test_one_card_field_by_field(self):
        p = self.by_id["3225932"]
        self.assertEqual(p.source_site, "homedy")
        self.assertEqual(
            p.fields["url"],
            "https://homedy.com/ban-can-ho-du-an-a/ban-can-ho-3pn-2wc-103m2-gia-28ty5-es3225932",
        )
        self.assertEqual(p.fields["title"], "Bán căn hộ 3PN, 2WC, 103m2, giá 28tỷ5")
        self.assertEqual(p.fields["price"], Decimal("28500000000"))
        self.assertEqual(p.fields["price_unit"], "tỷ")
        self.assertEqual(p.fields["area_sqm"], Decimal("103"))
        self.assertEqual(p.fields["price_per_sqm"], Decimal("276699029"))
        self.assertEqual(p.fields["district"], "Quận 1")
        self.assertEqual(p.fields["address_raw"], "Đường A, Phường B, quận 1, TP Hồ Chí Minh")
        self.assertEqual(p.fields["posted_date"], TODAY - timedelta(days=3))
        self.assertEqual(p.fields["property_type"], "apartment")
        self.assertEqual(p.fields["listing_intent"], "sale")

    def test_range_card_has_no_price_or_area(self):
        p = self.by_id["3161459"]
        self.assertIsNone(p.fields["price"])
        self.assertIsNone(p.fields["price_unit"])
        self.assertIsNone(p.fields["area_sqm"])
        self.assertIsNone(p.fields["price_per_sqm"])
        self.assertEqual(p.fields["district"], "Huyện Bình Chánh")

    def test_trieu_card_and_thu_duc_card(self):
        self.assertEqual(self.by_id["3226317"].fields["price"], Decimal("9000000"))
        self.assertEqual(self.by_id["3226317"].fields["price_unit"], "triệu")
        # price / area, never the card's rounded "0,1 tr/m2" text.
        self.assertEqual(self.by_id["3226317"].fields["price_per_sqm"], Decimal("138462"))
        self.assertEqual(self.by_id["3226317"].fields["district"], "Quận Gò Vấp")
        self.assertEqual(self.by_id["3226715"].fields["district"], "Thành phố Thủ Đức")
        self.assertEqual(self.by_id["3226715"].fields["posted_date"], TODAY)

    def test_no_agent_and_no_phone_field(self):
        for p in self.listings:
            with self.subTest(source_id=p.source_id):
                self.assertIsNone(p.agent_source_id)
                self.assertIsNone(p.agent_name)
                self.assertNotIn("phone_number", p.fields)


class ParseSrpCardTests(SimpleTestCase):
    def test_area_range_is_none(self):
        (p,), _ = _parse(_card("/ban-can-ho-du-an-x/can-ho-es7", area="55 - 120 m2"))
        self.assertIsNone(p.fields["area_sqm"])
        self.assertIsNone(p.fields["price_per_sqm"])

    def test_phone_in_href_and_title_is_cut_and_the_card_kept(self):
        (p,), skips = _parse(_card(
            "/ban-can-ho-du-an-x/chot-2-ty-goi-0901-234-567-es8",
            title="Chốt 2 tỷ GỌI 0901.234.567",
        ))
        self.assertEqual(skips, [])
        self.assertEqual(p.source_id, "8")
        self.assertEqual(p.fields["url"], "https://homedy.com/ban-can-ho-du-an-x/chot-2-ty-goi-es8")
        self.assertEqual(p.fields["title"], "Chốt 2 tỷ GỌI")
        for value in p.fields.values():
            self.assertNotRegex(str(value), r"901|234.?567")

    def test_phone_in_address_title_is_masked(self):
        (p,), _ = _parse(_card(
            "/ban-can-ho-du-an-x/can-ho-es9", address="Quận 7, LH 0901 234 567",
        ))
        self.assertNotIn("234", p.fields["address_raw"])

    def test_bad_cards_land_in_skips(self):
        listings, skips = _parse(
            _card("/ban-nha-rieng-quan-7/nha-pho-es10"),
            _card("/ban-can-ho-du-an-x/can-ho-khong-co-id"),
            _card("/ban-can-ho-du-an-x/can-ho-es11"),
        )
        self.assertEqual([p.source_id for p in listings], ["11"])
        self.assertEqual([field for _, field in skips], ["property_type", "source_id"])

    def test_skip_ref_never_carries_a_phone(self):
        _, skips = _parse(_card("/ban-nha-rieng/goi-0901-234-567-es12"))
        (ref, _), = skips
        self.assertNotIn("234", ref)

    def test_unmapped_district_is_none_not_raw_text(self):
        (p,), _ = _parse(_card("/ban-can-ho-du-an-x/can-ho-es13", address="Biên Hòa, Đồng Nai"))
        self.assertIsNone(p.fields["district"])

    def test_zero_cards_parse_to_empty_lists(self):
        self.assertEqual(_parse("<html><body>Không có tin</body></html>"), ([], []))


class PageUrlTests(SimpleTestCase):
    def test_page_one_is_the_root_and_later_pages_add_p_n(self):
        root = "/ban-can-ho-tp-ho-chi-minh"
        self.assertEqual(homedy.page_url(root, 1), "https://homedy.com/ban-can-ho-tp-ho-chi-minh")
        self.assertEqual(homedy.page_url(root, 2), "https://homedy.com/ban-can-ho-tp-ho-chi-minh/p2")
        self.assertEqual(homedy.CATEGORY_ROOTS, {root: ("apartment", "sale")})
