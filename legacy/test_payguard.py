import unittest
from payguard import authorize, available, capture, change_state, connect, issue, refund


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        issue(self.db, "c", "u", 10000)
        change_state(self.db, "c", "ACTIVE")

    def test_money_and_idempotency(self):
        auth = authorize(self.db, "a", "c", "shop", 1000, "k1")
        self.assertEqual(auth, authorize(self.db, "a", "c", "shop", 1000, "k1"))
        with self.assertRaises(ValueError):
            authorize(self.db, "a", "c", "shop", 2000, "k1")
        with self.assertRaises(ValueError):
            capture(self.db, "a", 100, "k1")
        self.assertEqual(available(self.db, "c"), 9000)
        capture(self.db, "a", 1000, "k2")
        self.assertEqual(available(self.db, "c"), 9000)
        refund(self.db, "a", 400, "k3")
        self.assertEqual(available(self.db, "c"), 9400)
        totals = self.db.execute("SELECT journal_id,sum(amount_cents) FROM postings GROUP BY journal_id").fetchall()
        self.assertTrue(all(total == 0 for _, total in totals))

    def test_invalid(self):
        with self.assertRaises(ValueError):
            change_state(self.db, "c", "CREATED")
        self.assertEqual(authorize(self.db, "big", "c", "shop", 60000, "x")["status"], "DECLINED")


if __name__ == "__main__":
    unittest.main()
