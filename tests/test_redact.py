"""Sensitive document text is masked before it can reach the model."""

from __future__ import annotations

import unittest

from app.safety import redact


class RedactTests(unittest.TestCase):
    def test_card_keeps_the_last_four_digits(self) -> None:
        masked = redact("Visa 4111-1111-1111-1111 and 4111 1111 1111 4242")
        self.assertIn("****1111", masked)
        self.assertIn("****4242", masked)
        self.assertNotIn("4111-1111-1111-1111", masked)
        self.assertNotIn("4111 1111 1111 4242", masked)

    def test_ssn_is_masked(self) -> None:
        masked = redact("SSN 123-45-6789 and 123 45 6789")
        self.assertEqual(masked.count("[REDACTED-SSN]"), 2)
        self.assertNotIn("123-45-6789", masked)
        self.assertNotIn("123 45 6789", masked)

    def test_sevis_id_is_masked(self) -> None:
        masked = redact("SEVIS N0012345678")
        self.assertIn("[REDACTED-SEVIS]", masked)
        self.assertNotIn("N0012345678", masked)

    def test_birth_date_is_masked_and_a_transaction_date_stays(self) -> None:
        masked = redact("DOB: 01/15/2004. Paid on 10/01/2026. Born January 2, 2001.")
        self.assertNotIn("01/15/2004", masked)
        self.assertNotIn("January 2, 2001", masked)
        self.assertIn("10/01/2026", masked)
        self.assertIn("[REDACTED-DOB]", masked)

    def test_email_and_phone_are_masked(self) -> None:
        masked = redact("Write a@yale.edu or call (203) 555-0100")
        self.assertNotIn("a@yale.edu", masked)
        self.assertNotIn("555-0100", masked)
        self.assertIn("[REDACTED-EMAIL]", masked)
        self.assertIn("[REDACTED-PHONE]", masked)

    def test_ordinary_merchant_line_stays_readable(self) -> None:
        line = "Starbucks 4.50 on 2026-10-01"
        self.assertEqual(redact(line), line)


if __name__ == "__main__":
    unittest.main()
