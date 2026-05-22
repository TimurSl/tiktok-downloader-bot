import sqlite3
import tempfile
import unittest
from pathlib import Path

from telegram_automation_bot.bot import RPStore


class RPStoreTests(unittest.TestCase):
    def test_commands_are_scoped_to_user_not_chat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RPStore(Path(tmp) / "bot.sqlite3")

            store.add(100, "hug", "{actor} hugs {target}")
            store.add(200, "hug", "{actor} waves to {target}")

            self.assertEqual(store.get(100, "hug"), "{actor} hugs {target}")
            self.assertEqual(store.get(200, "hug"), "{actor} waves to {target}")
            self.assertEqual(store.list(100), [("hug", "{actor} hugs {target}")])

    def test_migrates_chat_scoped_commands_to_created_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.sqlite3"
            with sqlite3.connect(path) as db:
                db.execute(
                    """
                    CREATE TABLE rp_commands (
                        chat_id TEXT NOT NULL,
                        command TEXT NOT NULL,
                        template TEXT NOT NULL,
                        created_by INTEGER,
                        created_at INTEGER NOT NULL,
                        PRIMARY KEY (chat_id, command)
                    )
                    """
                )
                db.execute(
                    """
                    INSERT INTO rp_commands (chat_id, command, template, created_by, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    ("chat-a", "hug", "old template", 100, 1),
                )
                db.execute(
                    """
                    INSERT INTO rp_commands (chat_id, command, template, created_by, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    ("chat-b", "hug", "new template", 100, 2),
                )
                db.execute(
                    """
                    INSERT INTO rp_commands (chat_id, command, template, created_by, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    ("chat-c", "poke", "ignored", None, 3),
                )

            store = RPStore(path)

            self.assertEqual(store.get(100, "hug"), "new template")
            self.assertIsNone(store.get(100, "poke"))


if __name__ == "__main__":
    unittest.main()
