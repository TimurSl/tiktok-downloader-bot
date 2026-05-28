import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import MethodType, SimpleNamespace

from telegram_automation_bot.bot import (
    AutomationBot,
    PUBLIC_BOT_API_UPLOAD_LIMIT_BYTES,
    PendingDownload,
    QualityOption,
    RPStore,
    best_audio_format,
    best_video_format,
    chunk_paths,
    combined_format_size,
    effective_upload_limit_bytes,
    extract_tiktok_image_urls,
    extract_tiktok_item_struct,
    extract_tiktok_rehydration_data,
    find_downloaded_files,
    is_group_chat,
    is_tiktok_photo_url,
    is_tiktok_redirect_url,
    is_tiktok_url,
    is_youtube_url,
    photo_album_chunks,
    quality_format_selector,
    quality_keyboard,
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

    def test_detects_tiktok_redirect_urls(self) -> None:
        self.assertTrue(is_tiktok_url("https://vm.tiktok.com/ZMh123/"))
        self.assertTrue(is_tiktok_url("https://vt.tiktok.com/ZMh123/"))
        self.assertTrue(is_tiktok_redirect_url("https://www.tiktok.com/t/ZMh123/"))
        self.assertFalse(is_tiktok_redirect_url("https://www.tiktok.com/@user/video/123"))


class MessageHandlingTests(unittest.TestCase):
    def test_group_media_links_bypass_user_allowlist(self) -> None:
        bot = AutomationBot.__new__(AutomationBot)
        bot.config = SimpleNamespace(authorized_users=frozenset({100}))
        bot.bot_username = "media_bot"
        bot.api = SimpleNamespace(send_message=lambda *args, **kwargs: None)
        downloads: list[str] = []

        def download_to_chat(self: AutomationBot, message: dict[str, object], url: str) -> None:
            downloads.append(url)

        bot.download_to_chat = MethodType(download_to_chat, bot)
        bot.handle_message(
            {
                "text": "@media_bot https://vm.tiktok.com/ZMh123/",
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 200},
                "message_id": 10,
            }
        )

        self.assertEqual(downloads, ["https://vm.tiktok.com/ZMh123/"])

    def test_private_media_links_still_require_allowlist(self) -> None:
        bot = AutomationBot.__new__(AutomationBot)
        bot.config = SimpleNamespace(authorized_users=frozenset({100}))
        bot.bot_username = "media_bot"
        bot.api = SimpleNamespace(send_message=lambda *args, **kwargs: None)
        downloads: list[str] = []
        bot.download_to_chat = MethodType(lambda _self, _message, url: downloads.append(url), bot)

        bot.handle_message(
            {
                "text": "https://vm.tiktok.com/ZMh123/",
                "chat": {"id": 200, "type": "private"},
                "from": {"id": 200},
                "message_id": 10,
            }
        )

        self.assertEqual(downloads, [])

    def test_rp_commands_with_urls_are_not_treated_as_downloads(self) -> None:
        bot = AutomationBot.__new__(AutomationBot)
        bot.config = SimpleNamespace(authorized_users=frozenset({100}))
        bot.bot_username = "media_bot"
        bot.api = SimpleNamespace(send_message=lambda *args, **kwargs: None)
        calls: list[str] = []
        bot.download_to_chat = MethodType(lambda _self, _message, url: calls.append(f"download:{url}"), bot)
        bot.handle_rp_command = MethodType(lambda _self, _message, text: calls.append(f"rp:{text}"), bot)

        bot.handle_message(
            {
                "text": "/rp add share {actor} shares https://example.test",
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 100},
                "message_id": 10,
            }
        )

        self.assertEqual(calls, ["rp:/rp add share {actor} shares https://example.test"])

    def test_group_chat_detection(self) -> None:
        self.assertTrue(is_group_chat({"chat": {"type": "group"}}))
        self.assertTrue(is_group_chat({"chat": {"type": "supergroup"}}))
        self.assertFalse(is_group_chat({"chat": {"type": "private"}}))


class YouTubeQualityTests(unittest.TestCase):
    def test_detects_youtube_urls(self) -> None:
        self.assertTrue(is_youtube_url("https://www.youtube.com/watch?v=abc"))
        self.assertTrue(is_youtube_url("https://youtu.be/abc"))
        self.assertFalse(is_youtube_url("https://example.test/watch?v=abc"))

    def test_quality_keyboard_uses_callback_data(self) -> None:
        keyboard = quality_keyboard(
            "abc123",
            [
                QualityOption("auto", "Auto", None),
                QualityOption("h720", "720p ~80 MB", quality_format_selector(720)),
            ],
        )

        self.assertEqual(
            keyboard,
            {
                "inline_keyboard": [
                    [{"text": "Auto", "callback_data": "dlq:abc123:auto"}],
                    [{"text": "720p ~80 MB", "callback_data": "dlq:abc123:h720"}],
                ]
            },
        )

    def test_estimates_merged_video_and_audio_size(self) -> None:
        formats = [
            {"format_id": "18", "height": 360, "vcodec": "avc1", "acodec": "mp4a", "filesize": 12},
            {"format_id": "136", "height": 720, "vcodec": "avc1", "acodec": "none", "filesize": 100},
            {"format_id": "140", "vcodec": "none", "acodec": "mp4a", "filesize": 20, "abr": 128},
        ]

        video = best_video_format(formats, max_height=720)
        audio = best_audio_format(formats)

        self.assertEqual(video["format_id"], "136")
        self.assertEqual(audio["format_id"], "140")
        self.assertEqual(combined_format_size(video, audio, 120), 120)

    def test_callback_downloads_selected_quality(self) -> None:
        bot = AutomationBot.__new__(AutomationBot)
        bot.config = SimpleNamespace(authorized_users=frozenset())
        bot.bot_username = "media_bot"
        bot.pending_downloads = {
            "token": PendingDownload(
                url="https://www.youtube.com/watch?v=abc",
                chat_id=-100,
                reply_to_message_id=10,
                requester_id=200,
                created_at=time.time(),
                options={
                    "h720": QualityOption("h720", "720p ~80 MB", quality_format_selector(720)),
                },
            )
        }
        bot.api = FakeTelegramAPI()
        calls: list[tuple[int, str, int | None, str | None]] = []
        bot.download_to_chat_url = MethodType(
            lambda _self, chat_id, url, *, reply_to_message_id=None, format_selector=None: calls.append(
                (chat_id, url, reply_to_message_id, format_selector)
            ),
            bot,
        )

        bot.handle_callback_query(
            {
                "id": "callback-id",
                "from": {"id": 200},
                "data": "dlq:token:h720",
                "message": {"message_id": 99, "chat": {"id": -100}},
            }
        )

        self.assertEqual(calls, [(-100, "https://www.youtube.com/watch?v=abc", 10, quality_format_selector(720))])
        self.assertNotIn("token", bot.pending_downloads)
        self.assertEqual(bot.api.callback_answers, [("callback-id", "Downloading 720p ~80 MB...")])
        self.assertEqual(bot.api.edits, [(-100, 99, "Downloading 720p ~80 MB...")])


class FakeTelegramAPI:
    def __init__(self) -> None:
        self.callback_answers: list[tuple[str, str]] = []
        self.edits: list[tuple[int, int, str]] = []

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> bool:
        self.callback_answers.append((callback_query_id, text))
        return True

    def edit_message_text(self, chat_id: int, message_id: int, text: str, **_kwargs: object) -> bool:
        self.edits.append((chat_id, message_id, text))
        return True


class UploadLimitTests(unittest.TestCase):
    def test_public_bot_api_clamps_configured_limit_to_telegram_upload_cap(self) -> None:
        config = SimpleNamespace(
            api_base="https://api.telegram.org",
            max_upload_bytes=100 * 1024 * 1024,
        )

        self.assertEqual(effective_upload_limit_bytes(config), PUBLIC_BOT_API_UPLOAD_LIMIT_BYTES)

    def test_custom_bot_api_keeps_configured_limit(self) -> None:
        config = SimpleNamespace(
            api_base="http://127.0.0.1:8081",
            max_upload_bytes=100 * 1024 * 1024,
        )

        self.assertEqual(effective_upload_limit_bytes(config), 100 * 1024 * 1024)

    def test_ensure_size_rejects_public_bot_api_file_before_upload(self) -> None:
        bot = AutomationBot.__new__(AutomationBot)
        bot.config = SimpleNamespace(
            api_base="https://api.telegram.org",
            max_upload_bytes=100 * 1024 * 1024,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "video.mp4"
            with path.open("wb") as handle:
                handle.truncate(PUBLIC_BOT_API_UPLOAD_LIMIT_BYTES + 1)

            with self.assertRaisesRegex(RuntimeError, "max upload is 50 MB"):
                bot.ensure_size(path)


if __name__ == "__main__":
    unittest.main()
