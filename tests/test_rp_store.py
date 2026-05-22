import sqlite3
import tempfile
import unittest
from pathlib import Path

from telegram_automation_bot.bot import (
    RPStore,
    chunk_paths,
    extract_tiktok_image_urls,
    extract_tiktok_item_struct,
    extract_tiktok_rehydration_data,
    find_downloaded_files,
    is_tiktok_photo_url,
    photo_album_chunks,
    select_downloaded_media,
    tiktok_photo_id,
)


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


class DownloadedMediaTests(unittest.TestCase):
    def test_photo_outputs_are_kept_in_natural_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for name in ["post-10.jpg", "post-2.jpg", "post-1.jpg", "cookies.txt"]:
                (directory / name).write_bytes(b"data")

            paths = select_downloaded_media(find_downloaded_files(directory))

            self.assertEqual([path.name for path in paths], ["post-1.jpg", "post-2.jpg", "post-10.jpg"])

    def test_non_photo_outputs_keep_largest_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            small = directory / "audio.m4a"
            large = directory / "video.mp4"
            small.write_bytes(b"1")
            large.write_bytes(b"1" * 20)

            paths = select_downloaded_media(find_downloaded_files(directory))

            self.assertEqual(paths, (large,))

    def test_chunk_paths_splits_media_groups_at_ten_items(self) -> None:
        paths = tuple(Path(f"image-{index}.jpg") for index in range(23))

        chunks = chunk_paths(paths, 10)

        self.assertEqual([len(chunk) for chunk in chunks], [10, 10, 3])

    def test_photo_album_chunks_avoid_single_photo_tail(self) -> None:
        paths = tuple(Path(f"image-{index}.jpg") for index in range(11))

        chunks = photo_album_chunks(paths)

        self.assertEqual([len(chunk) for chunk in chunks], [9, 2])

    def test_extracts_tiktok_photo_post_images_from_rehydration_json(self) -> None:
        webpage = """
        <html>
          <script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">
          {
            "__DEFAULT_SCOPE__": {
              "webapp.video-detail": {
                "itemInfo": {
                  "itemStruct": {
                    "desc": "photo set",
                    "imagePost": {
                      "images": [
                        {"imageURL": {"urlList": ["https://example.test/1.jpeg"]}},
                        {"imageURL": {"urlList": ["https://example.test/2.jpeg"]}}
                      ]
                    }
                  }
                }
              }
            }
          }
          </script>
        </html>
        """

        data = extract_tiktok_rehydration_data(webpage)
        item = extract_tiktok_item_struct(data)
        image_urls = extract_tiktok_image_urls(item)

        self.assertEqual(item["desc"], "photo set")
        self.assertEqual(
            image_urls,
            ("https://example.test/1.jpeg", "https://example.test/2.jpeg"),
        )

    def test_detects_tiktok_photo_url_with_query_string(self) -> None:
        url = "https://www.tiktok.com/@ghvst.1/photo/7630229704976502037?_r=1&_t=ZS-96ZiGadoWoO"

        self.assertTrue(is_tiktok_photo_url(url))
        self.assertEqual(tiktok_photo_id(url), "7630229704976502037")


if __name__ == "__main__":
    unittest.main()
