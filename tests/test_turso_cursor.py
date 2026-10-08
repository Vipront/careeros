import unittest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

class FakeResultSet:
    def __init__(self, columns, rows, rows_affected=0):
        self.columns = columns
        self.rows = rows
        self.rows_affected = rows_affected

class TestTursoCursor(unittest.TestCase):
    def test_cursor_iteration(self):
        """Ensure TursoCursor can be iterated over like sqlite3.Cursor."""
        from src.db import TursoCursor
        fake_res = FakeResultSet(columns=["id", "name"], rows=[(1, "Job A"), (2, "Job B")])
        cursor = TursoCursor(fake_res)

        # Direct loop check: for row in cursor
        yielded = []
        for row in cursor:
            yielded.append(row)

        self.assertEqual(yielded, [(1, "Job A"), (2, "Job B")])

    def test_cursor_iteration_comprehension(self):
        """Ensure comprehension works like {r[1] for r in con.execute(...)} in verify_setup.py."""
        from src.db import TursoCursor
        fake_res = FakeResultSet(columns=["cid", "name"], rows=[(0, "fingerprint"), (1, "title")])
        cursor = TursoCursor(fake_res)

        names = {r[1] for r in cursor}
        self.assertEqual(names, {"fingerprint", "title"})

    def test_cursor_iteration_empty(self):
        """Ensure empty TursoCursor produces empty iterator without error."""
        from src.db import TursoCursor
        fake_empty = FakeResultSet(columns=["id"], rows=[])
        cursor = TursoCursor(fake_empty)
        yielded = list(cursor)
        self.assertEqual(yielded, [])

        cursor_none = TursoCursor(None)
        yielded_none = list(cursor_none)
        self.assertEqual(yielded_none, [])

    def test_cursor_existing_methods_preserved(self):
        """Ensure fetchone, fetchall, description and rowcount still work as expected."""
        from src.db import TursoCursor
        fake_res = FakeResultSet(columns=["id", "title"], rows=[(10, "Scientist")], rows_affected=1)
        cursor = TursoCursor(fake_res)

        self.assertEqual(cursor.fetchone(), (10, "Scientist"))
        self.assertEqual(cursor.fetchall(), [(10, "Scientist")])
        self.assertEqual(cursor.rowcount, 1)
        self.assertEqual(cursor.description[0][0], "id")
        self.assertEqual(cursor.description[1][0], "title")

if __name__ == "__main__":
    unittest.main()
