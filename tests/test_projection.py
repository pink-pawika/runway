"""Pace math for the dashboard. Dates are fixed so the result does not depend on today."""

from __future__ import annotations

import unittest
from datetime import date

from app.projection import classify_spending, project_category


class ProjectionTests(unittest.TestCase):
    def test_day_3_returns_null(self) -> None:
        early = project_category(
            category="Dining out",
            limit_cents=30000,
            committed_cents=0,
            discretionary_cents=9000,
            month="2026-09",
            today=date(2026, 9, 3),
        )
        self.assertIsNone(early)
        other_month = project_category(
            category="Dining out",
            limit_cents=30000,
            committed_cents=0,
            discretionary_cents=9000,
            month="2026-08",
            today=date(2026, 9, 10),
        )
        self.assertIsNone(other_month)

    def test_normal_pace_names_the_hit_date(self) -> None:
        result = project_category(
            category="Dining out",
            limit_cents=30000,
            committed_cents=0,
            discretionary_cents=9000,
            month="2026-09",
            today=date(2026, 9, 6),
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["daily_rate_cents"], 1500)
        self.assertEqual(result["projected_cents"], 45000)
        self.assertEqual(result["projected_percent"], 1.5)
        self.assertEqual(result["hit_date"], "2026-09-20")
        self.assertEqual(result["days_early"], 10)
        self.assertTrue(result["at_risk"])
        self.assertEqual(
            result["message"],
            "At this pace, Dining out will hit its limit on the 20th (10 days early). "
            "Projected: $450 of $300.",
        )
        labels = [line["label"] for line in result["explain"]]
        self.assertEqual(labels, ["Spent so far", "Committed", "Days elapsed", "Daily rate"])

    def test_already_over_budget_has_no_hit_date(self) -> None:
        result = project_category(
            category="Dining out",
            limit_cents=30000,
            committed_cents=0,
            discretionary_cents=40000,
            month="2026-09",
            today=date(2026, 9, 10),
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIsNone(result["hit_date"])
        self.assertEqual(result["projected_cents"], 120000)
        self.assertTrue(result["at_risk"])
        self.assertEqual(
            result["message"],
            "Dining out is already over its limit. Projected: $1,200 of $300.",
        )

    def test_zero_spending_stays_inside_the_limit(self) -> None:
        result = project_category(
            category="Dining out",
            limit_cents=30000,
            committed_cents=0,
            discretionary_cents=0,
            month="2026-09",
            today=date(2026, 9, 10),
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["daily_rate_cents"], 0)
        self.assertEqual(result["projected_cents"], 0)
        self.assertEqual(result["projected_percent"], 0)
        self.assertIsNone(result["hit_date"])
        self.assertFalse(result["at_risk"])
        self.assertEqual(
            result["message"],
            "At this pace, Dining out stays within budget. Projected: $0 of $300.",
        )

    def test_last_day_does_not_add_more_days(self) -> None:
        result = project_category(
            category="Dining out",
            limit_cents=30000,
            committed_cents=0,
            discretionary_cents=15000,
            month="2026-09",
            today=date(2026, 9, 30),
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["days_remaining"], 0)
        self.assertEqual(result["projected_cents"], 15000)
        self.assertIsNone(result["hit_date"])
        self.assertFalse(result["at_risk"])
        self.assertEqual(
            result["message"],
            "At this pace, Dining out stays within budget. Projected: $150 of $300.",
        )

    def test_expected_and_rent_are_committed(self) -> None:
        self.assertEqual(classify_spending("expected", "Groceries"), "committed")
        self.assertEqual(classify_spending("confirmed", "Rent & utilities"), "committed")
        self.assertEqual(classify_spending("confirmed", "Tuition & fees"), "committed")
        self.assertEqual(classify_spending("confirmed", "Health insurance"), "committed")
        self.assertEqual(classify_spending("confirmed", "Dining out"), "discretionary")
        self.assertIsNone(classify_spending("review", "Rent & utilities"))
        self.assertIsNone(classify_spending("cancelled", "Dining out"))


if __name__ == "__main__":
    unittest.main()
