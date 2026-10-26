"""Merchant keys used by merchant_rules."""

from __future__ import annotations

import unittest

from app.categories import normalize_merchant


class NormalizeMerchantTests(unittest.TestCase):
    def test_starbucks_store_numbers_share_one_key(self) -> None:
        self.assertEqual(normalize_merchant("STARBUCKS #1234"), "starbucks")
        self.assertEqual(normalize_merchant("Starbucks Store 55"), "starbucks")
        self.assertEqual(
            normalize_merchant("STARBUCKS #1234"),
            normalize_merchant("Starbucks Store 55"),
        )

    def test_company_suffixes_are_removed(self) -> None:
        self.assertEqual(normalize_merchant("Netflix, Inc."), "netflix")
        self.assertEqual(normalize_merchant("Netflix LLC"), "netflix")

    def test_apostrophes_do_not_split_the_name(self) -> None:
        self.assertEqual(normalize_merchant("McDonald's #4821"), "mcdonalds")

    def test_blank_name_is_an_empty_key(self) -> None:
        self.assertEqual(normalize_merchant("   "), "")
        self.assertEqual(normalize_merchant("#1234"), "")

    def test_meaningful_words_stay_in_the_key(self) -> None:
        self.assertEqual(normalize_merchant("Uber Eats"), "uber eats")
        self.assertNotEqual(normalize_merchant("Uber Eats"), normalize_merchant("Uber"))


if __name__ == "__main__":
    unittest.main()
