"""No-network tests for the Lite research filter, confidentiality and scope."""
import unittest
from datetime import datetime, timedelta, timezone

import betting_lab_lite_app as lite


def event(*, update=None):
    at = (update or datetime.now(timezone.utc)).isoformat()
    return {
        "id":"euro-game-1","sport_key":"basketball_euroleague",
        "home_team":"Euro Home","away_team":"Euro Away",
        "bookmakers":[
            {"key":"williamhill","title":"William Hill","last_update":at,
             "markets":[{"key":"h2h","outcomes":[
                 {"name":"Euro Home","price":1.90},{"name":"Euro Away","price":1.92}
             ]}]},
            {"key":"ref_book_a","title":"Reference A","last_update":at,
             "markets":[{"key":"h2h","outcomes":[
                 {"name":"Euro Home","price":1.42},{"name":"Euro Away","price":3.1}
             ]}]},
            {"key":"ref_book_b","title":"Reference B","last_update":at,
             "markets":[{"key":"h2h","outcomes":[
                 {"name":"Euro Home","price":1.46},{"name":"Euro Away","price":2.95}
             ]}]},
        ],
    }


class LiteSelectionTests(unittest.TestCase):
    def test_only_euroleague_two_prespecified_bands(self):
        self.assertEqual(lite.SPORT,"basketball_euroleague")
        self.assertEqual(lite.BANDS,((1.5,2.0),(3.0,4.0)))
        for number in (1.49,2.0,2.5,4.0,10.0):
            self.assertFalse(lite.propose(event(), {
                "williamhill":("William Hill",{"Euro Home":number,"Euro Away":1.92})
            }))

    def test_one_frozen_pick_uses_external_consensus(self):
        ev=event()
        quotes=lite.verified_books(ev,datetime.now(timezone.utc))
        pick=lite.propose(ev,quotes)
        self.assertIsNotNone(pick)
        self.assertEqual(pick["selection"],"Euro Home")
        self.assertEqual(pick["bookmaker_key"],"williamhill")
        self.assertEqual(pick["ref_count"],2)
        self.assertGreater(pick["edge"],4)

    def test_missing_or_stale_reference_blocks_signal(self):
        ev=event()
        ev["bookmakers"]=ev["bookmakers"][:2]
        self.assertIsNone(lite.propose(ev,lite.verified_books(ev,datetime.now(timezone.utc))))
        ev=event(update=datetime.now(timezone.utc)-timedelta(hours=2))
        self.assertEqual(lite.verified_books(ev,datetime.now(timezone.utc)),{})

    def test_opp_fixture_market_not_accepted(self):
        ev=event()
        ev["bookmakers"][1]["markets"][0]["key"]="totals"
        books=lite.verified_books(ev,datetime.now(timezone.utc))
        self.assertNotIn("ref_book_a",books)
        self.assertIsNone(lite.propose(ev,books))

    def test_free_plan_credit_cap_and_paid_cutoff(self):
        before=datetime(2026,12,14,23,59,tzinfo=timezone.utc)
        after=datetime(2026,12,15,0,0,tzinfo=timezone.utc)
        self.assertEqual(lite.credit_cap(before),lite.PAID_CAP)
        self.assertEqual(lite.credit_cap(after),lite.FREE_CAP)
        self.assertLessEqual(lite.FREE_CAP,12)
        self.assertGreaterEqual(lite.ODDS_HOURS,4)


    def test_paid_quote_polling_targets_closing_prices_without_burning_idle_credits(self):
        from unittest.mock import patch
        before=datetime(2026,12,14,12,tzinfo=timezone.utc)
        for ahead, expected in ((2,10),(20,20),(48,60)):
            with patch.object(lite,"db") as db:
                db.fetchone.return_value={"next_game":(before+timedelta(hours=ahead)).isoformat()}
                self.assertEqual(round(lite.odds_poll_hours(before)*60),expected)
        with patch.object(lite,"db") as db:
            db.fetchone.return_value={"next_game":None}
            self.assertEqual(round(lite.odds_poll_hours(before)*60),60)
        after=datetime(2026,12,15,12,tzinfo=timezone.utc)
        with patch.object(lite,"db") as db:
            self.assertGreaterEqual(lite.odds_poll_hours(after),6)
            db.fetchone.assert_not_called()
        self.assertGreater(lite.PAID_CAP,30)

    def test_provider_balance_probe_is_zero_cost(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        with patch.object(lite,"api") as api, patch.object(lite,"record") as record:
            api.api_key="test-only"
            api.sports.return_value=SimpleNamespace(remaining=98765,used=1235)
            lite.probe_provider_balance()
            api.sports.assert_called_once_with()
            self.assertEqual(record.call_args.args[3],0)
            self.assertIn("provider_remaining=98765",record.call_args.args[4])

    def test_live_execution_components_are_not_imported(self):
        import sys
        self.assertNotIn("web",sys.modules)
        self.assertNotIn("worker",sys.modules)
        self.assertNotIn("telegram_alerts",sys.modules)

if __name__=="__main__":
    unittest.main()
