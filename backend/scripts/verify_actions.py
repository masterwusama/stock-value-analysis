import unittest
from datetime import date, datetime
from decimal import Decimal
from itertools import product
from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

with patch("app.config.DATABASE_URL", "sqlite+pysqlite:///:memory:"):
    from app.api.securities import BuybackCell, _action_picks, _actions_cell, _buy_cell
from app.models import ShareAction


def buy_row(**changes):
    row = dict(
        src_id="buy-1", notice_date=date(2026, 3, 1), finish_date=None,
        progress_label="实施中", finished=False, cancel_type="注销",
        done_price=None, done_num=None, done_amount=None,
        plan_price_cap=Decimal("20"), plan_num_lower=Decimal("800"),
        plan_num_cap=Decimal("1200"), plan_amount_lower=Decimal("8000"),
        plan_amount_cap=Decimal("24000"),
    )
    row.update(changes)
    return row


class ActionCellTests(unittest.TestCase):
    def assert_values(self, cell, values, actual):
        payload = cell.model_dump()
        self.assertEqual(
            tuple(payload[k] for k in ("price", "num", "num_lo", "amount", "amount_lo")),
            values,
        )
        for field, expected in zip(("price_actual", "num_actual", "amount_actual"), actual):
            self.assertIs(payload[field], expected)
        self.assertNotIn("executed", payload)

    def test_buyback_defaults(self):
        self.assert_values(BuybackCell(src_id="empty"), (None,) * 5, (False,) * 3)

    def test_planned(self):
        cell = _buy_cell(buy_row(progress_label="预案"))
        self.assert_values(cell, (20, 1200, 800, 24000, 8000), (False,) * 3)
        self.assertIs(cell.finished, False)
        self.assertEqual((cell.src_id, cell.progress, cell.cancel_type),
                         ("buy-1", "预案", "注销"))

    def test_partial_execution_and_completion(self):
        for finished in (False, True):
            with self.subTest(finished=finished):
                cell = _buy_cell(buy_row(
                    finished=finished, done_price=Decimal("10.5"),
                    done_num=Decimal("500"), done_amount=Decimal("5250"),
                ))
                self.assert_values(cell, (10.5, 500, None, 5250, None), (True,) * 3)
                self.assertIs(cell.finished, finished)

    def test_mixed_actual_fields_are_independent(self):
        for finished, price_actual, num_actual, amount_actual in product((False, True), repeat=4):
            with self.subTest(finished=finished, actual=(price_actual, num_actual, amount_actual)):
                cell = _buy_cell(buy_row(
                    finished=finished,
                    done_price=Decimal("10.5") if price_actual else None,
                    done_num=Decimal("500") if num_actual else None,
                    done_amount=Decimal("5250") if amount_actual else None,
                ))
                self.assert_values(cell, (
                    10.5 if price_actual else 20,
                    500 if num_actual else 1200, None if num_actual else 800,
                    5250 if amount_actual else 24000, None if amount_actual else 8000,
                ), (price_actual, num_actual, amount_actual))
                self.assertIs(cell.finished, finished)

    def test_zero_actuals_are_not_missing(self):
        for zero in (0, Decimal("0")):
            with self.subTest(zero=repr(zero)):
                cell = _buy_cell(buy_row(done_price=zero, done_num=zero, done_amount=zero))
                self.assert_values(cell, (0, 0, None, 0, None), (True,) * 3)

    def test_zero_plan_values_are_not_missing(self):
        cell = _buy_cell(buy_row(**{k: Decimal("0") for k in buy_row() if k.startswith("plan_")}))
        self.assert_values(cell, (0,) * 5, (False,) * 3)

    def test_missing_all_values(self):
        row = {key: None for key in buy_row()}
        row["src_id"] = "missing"
        cell = _buy_cell(row)
        self.assert_values(cell, (None,) * 5, (False,) * 3)
        self.assertIs(cell.finished, False)
        self.assertIsNone(cell.date)
        self.assertIsNone(cell.progress)
        self.assertIsNone(cell.cancel_type)

    def test_date_depends_only_on_slot(self):
        for finished, notice, finish in product(
            (False, True), (None, date(2026, 3, 1)), (None, date(2026, 4, 2)),
        ):
            with self.subTest(finished=finished, notice=notice, finish=finish):
                row = buy_row(finished=finished, notice_date=notice, finish_date=finish)
                self.assertEqual(_buy_cell(row).date, notice)
                self.assertEqual(_buy_cell(row, done_slot=True).date, finish or notice)

    def test_actions_same_source_fills_both_slots(self):
        row = buy_row(finished=True, finish_date=date(2026, 4, 2), done_price=Decimal("10.5"))
        cell = _actions_cell({"buy_latest": row, "buy_done": row})
        self.assertIsNone(cell.seo)
        self.assertEqual(cell.buy_latest.src_id, cell.buy_done.src_id)
        self.assertEqual(cell.buy_latest.date, row["notice_date"])
        self.assertEqual(cell.buy_done.date, row["finish_date"])
        self.assertEqual(cell.buy_latest.model_dump(exclude={"date"}),
                         cell.buy_done.model_dump(exclude={"date"}))
        for buy in (cell.buy_latest, cell.buy_done):
            self.assert_values(buy, (10.5, 1200, 800, 24000, 8000), (True, False, False))

    def test_actions_seo_dates_and_values(self):
        for issue in (None, date(2026, 1, 2)):
            with self.subTest(issue=issue):
                row = dict(issue_date=issue, listing_date=date(2026, 2, 3),
                           price=Decimal("12.5"), num=Decimal("100"), raise_funds=Decimal("1250"))
                cell = _actions_cell({"seo": row})
                self.assertEqual(cell.seo.model_dump(), dict(
                    date=issue or row["listing_date"], price=12.5, num=100, amount=1250,
                ))
                self.assertIsNone(cell.buy_latest)
                self.assertIsNone(cell.buy_done)

    def test_empty_picks(self):
        self.assertEqual(_actions_cell({}).model_dump(),
                         {"seo": None, "buy_latest": None, "buy_done": None})


class ActionPickTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        self.addCleanup(engine.dispose)
        ShareAction.__table__.create(engine)
        self.db = Session(engine)
        self.addCleanup(self.db.close)

    def add_action(self, src_id, sid=1, kind="buyback", **changes):
        values = dict(sid=sid, kind=kind, src_id=src_id, finished=False,
                      updated_at=datetime(2026, 1, 1))
        values.update(changes)
        self.db.add(ShareAction(**values))
        self.db.flush()

    def test_latest_and_older_completed_use_different_dates(self):
        self.add_action("latest", notice_date=date(2026, 6, 1),
                        finish_date=date(2026, 7, 1), done_num=100)
        self.add_action("recent-completion", finished=True,
                        notice_date=date(2026, 1, 1), finish_date=date(2026, 5, 1))
        self.add_action("recent-notice", finished=True,
                        notice_date=date(2026, 4, 1), finish_date=date(2026, 4, 2))
        self.add_action("unknown-status", finished=None, notice_date=date(2026, 2, 1),
                        finish_date=date(2026, 8, 1))
        picks = _action_picks(self.db, [1])
        self.assertEqual(set(picks), {1})
        self.assertEqual({slot: row["src_id"] for slot, row in picks[1].items()},
                         {"buy_latest": "latest", "buy_done": "recent-completion"})

    def test_latest_completed_fills_both_slots(self):
        self.add_action("old", finished=True, notice_date=date(2026, 1, 1),
                        finish_date=date(2026, 2, 1))
        self.add_action("new", finished=True, notice_date=date(2026, 4, 1),
                        finish_date=date(2026, 5, 1))
        picks = _action_picks(self.db, [1])[1]
        self.assertEqual(set(picks), {"buy_latest", "buy_done"})
        self.assertEqual(picks["buy_latest"]["src_id"], "new")
        self.assertEqual(picks["buy_done"]["src_id"], "new")

    def test_other_sid_is_isolated(self):
        self.add_action("shared-id", notice_date=date(2026, 1, 1))
        self.add_action("shared-id", sid=2, finished=True, notice_date=date(2026, 8, 1))
        picks = _action_picks(self.db, [1])
        self.assertEqual(set(picks), {1})
        self.assertEqual(set(picks[1]), {"buy_latest"})
        picks = _action_picks(self.db, [1, 2])
        self.assertEqual(set(picks), {1, 2})
        self.assertEqual(set(picks[1]), {"buy_latest"})
        self.assertEqual(set(picks[2]), {"buy_latest", "buy_done"})
        for sid, slots in picks.items():
            self.assertTrue(all(row["sid"] == sid for row in slots.values()))

    def test_seo_uses_issue_date_then_listing_date(self):
        self.add_action("buy", notice_date=date(2026, 8, 1))
        self.add_action("seo-old", kind="seo", finished=True,
                        issue_date=date(2026, 1, 1), listing_date=date(2026, 6, 1))
        self.add_action("seo-latest", kind="seo", listing_date=date(2026, 4, 1))
        picks = _action_picks(self.db, [1])[1]
        self.assertEqual({slot: row["src_id"] for slot, row in picks.items()},
                         {"seo": "seo-latest", "buy_latest": "buy"})

    def test_buyback_date_fallbacks(self):
        self.add_action("old", finished=True, notice_date=date(2026, 1, 1))
        self.add_action("updated", finished=True, updated_at=datetime(2026, 3, 1, 12))
        self.add_action("notice", finished=True, notice_date=date(2026, 2, 1),
                        updated_at=datetime(2026, 4, 1))
        picks = _action_picks(self.db, [1])[1]
        self.assertEqual(picks["buy_latest"]["src_id"], "updated")
        self.assertEqual(picks["buy_done"]["src_id"], "updated")

    def test_no_rows(self):
        self.assertEqual(_action_picks(self.db, [1]), {})
        self.add_action("other", sid=2)
        self.assertEqual(_action_picks(self.db, [1]), {})

    def test_empty_sids_do_not_query(self):
        db = Mock(spec=Session)
        self.assertEqual(_action_picks(db, []), {})
        db.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
