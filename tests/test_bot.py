import tempfile
import time
import unittest
from pathlib import Path
from types import MethodType, SimpleNamespace

from telegram_automation_bot.bot import (
    AutomationBot,
    ChatContext,
    DownloadSession,
    DownloadedMedia,
    MediaDownloader,
    PUBLIC_BOT_API_UPLOAD_LIMIT_BYTES,
    QualityOption,
    TelegramAPIError,
    best_audio_format,
    best_video_format,
    chunk_paths,
    combined_format_size,
    effective_upload_limit_bytes,
    extract_tiktok_image_urls,
    extract_tiktok_item_struct,
    extract_tiktok_rehydration_data,
    find_downloaded_files,
    first_download_url,
    friendly_error_message,
    is_group_chat,
    is_downloadable_url,
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

    def test_sidecars_and_unsupported_outputs_are_not_selected_for_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            video = directory / "video.mp4"
            archive = directory / "archive.zip"
            info = directory / "video.info.json"
            subtitles = directory / "video.en.vtt"
            video.write_bytes(b"1" * 20)
            archive.write_bytes(b"1" * 50)
            info.write_text("{}", encoding="utf-8")
            subtitles.write_text("WEBVTT", encoding="utf-8")

            paths = select_downloaded_media(find_downloaded_files(directory))

            self.assertEqual(paths, (video,))

    def test_chunk_paths_splits_media_groups_at_ten_items(self) -> None:
        paths = tuple(Path(f"image-{index}.jpg") for index in range(23))

        chunks = chunk_paths(paths, 10)

        self.assertEqual([len(chunk) for chunk in chunks], [10, 10, 3])

    def test_photo_album_chunk_sizes_for_telegram_album_limits(self) -> None:
        cases = {
            9: [9],
            10: [10],
            11: [9, 2],
            23: [10, 10, 3],
        }

        for count, expected_sizes in cases.items():
            with self.subTest(count=count):
                paths = tuple(Path(f"image-{index}.jpg") for index in range(count))
                self.assertEqual([len(chunk) for chunk in photo_album_chunks(paths)], expected_sizes)


class DownloaderStrategyTests(unittest.TestCase):
    def test_download_strategies_try_requested_then_fallback_formats(self) -> None:
        downloader = MediaDownloader.__new__(MediaDownloader)
        downloader.config = SimpleNamespace(ytdlp_format="bv*+ba/best")

        strategies = downloader.download_strategies("bv*[height<=720]+ba/b[height<=720]/best")

        self.assertEqual(strategies[0].name, "requested")
        self.assertEqual(strategies[0].format_selector, "bv*[height<=720]+ba/b[height<=720]/best")
        self.assertIn("bestvideo*+bestaudio/bestvideo+bestaudio/best", [strategy.format_selector for strategy in strategies])
        self.assertIn("best", [strategy.format_selector for strategy in strategies])

    def test_download_strategies_dedupe_configured_default(self) -> None:
        downloader = MediaDownloader.__new__(MediaDownloader)
        downloader.config = SimpleNamespace(ytdlp_format="best")

        strategies = downloader.download_strategies(None)

        self.assertEqual([strategy.format_selector for strategy in strategies].count("best"), 1)


class TikTokMetadataTests(unittest.TestCase):
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

    def test_extracts_tiktok_photo_images_from_metadata_variants_and_dedupes(self) -> None:
        item = {
            "desc": "variant set",
            "image_post_info": {
                "image_list": [
                    {"display_image": {"url_list": ["https://example.test/a.webp"]}},
                    {"imageURL": {"urlList": ["https://example.test/a.webp"]}},
                    {"nested": {"urls": ["//example.test/b.jpeg"]}},
                ]
            },
        }

        self.assertEqual(
            extract_tiktok_image_urls(item),
            ("https://example.test/a.webp", "//example.test/b.jpeg"),
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
    def test_authorized_chat_ignores_non_media_links(self) -> None:
        bot = AutomationBot.__new__(AutomationBot)
        bot.config = SimpleNamespace(authorized_users=frozenset())
        bot.bot_username = "media_bot"
        bot.api = FakeTelegramAPI()
        downloads: list[str] = []
        bot.prepare_download_to_chat = MethodType(lambda _self, _message, url: downloads.append(url), bot)

        bot.handle_message(
            {
                "text": "please read https://example.test/docs instead",
                "chat": {"id": 200, "type": "private"},
                "from": {"id": 200},
                "message_id": 10,
            }
        )

        self.assertEqual(downloads, [])

    def test_message_downloads_later_media_link_instead_of_first_ordinary_link(self) -> None:
        bot = AutomationBot.__new__(AutomationBot)
        bot.config = SimpleNamespace(authorized_users=frozenset())
        bot.bot_username = "media_bot"
        bot.api = FakeTelegramAPI()
        downloads: list[str] = []
        bot.prepare_download_to_chat = MethodType(lambda _self, _message, url: downloads.append(url), bot)

        bot.handle_message(
            {
                "text": "context https://example.test/docs video https://www.instagram.com/reel/abc123/",
                "chat": {"id": 200, "type": "private"},
                "from": {"id": 200},
                "message_id": 10,
            }
        )

        self.assertEqual(downloads, ["https://www.instagram.com/reel/abc123/"])

    def test_group_ordinary_links_do_not_bypass_user_allowlist(self) -> None:
        bot = AutomationBot.__new__(AutomationBot)
        bot.config = SimpleNamespace(authorized_users=frozenset({100}))
        bot.bot_username = "media_bot"
        bot.api = FakeTelegramAPI()
        downloads: list[str] = []
        bot.prepare_download_to_chat = MethodType(lambda _self, _message, url: downloads.append(url), bot)

        bot.handle_message(
            {
                "text": "https://example.test/docs",
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 200},
                "message_id": 10,
            }
        )

        self.assertEqual(downloads, [])

    def test_group_media_links_bypass_user_allowlist(self) -> None:
        bot = AutomationBot.__new__(AutomationBot)
        bot.config = SimpleNamespace(authorized_users=frozenset({100}))
        bot.bot_username = "media_bot"
        bot.api = FakeTelegramAPI()
        downloads: list[str] = []

        def prepare_download(self: AutomationBot, message: dict[str, object], url: str) -> None:
            downloads.append(url)

        bot.prepare_download_to_chat = MethodType(prepare_download, bot)
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
        bot.api = FakeTelegramAPI()
        downloads: list[str] = []
        bot.prepare_download_to_chat = MethodType(lambda _self, _message, url: downloads.append(url), bot)

        bot.handle_message(
            {
                "text": "https://vm.tiktok.com/ZMh123/",
                "chat": {"id": 200, "type": "private"},
                "from": {"id": 200},
                "message_id": 10,
            }
        )

        self.assertEqual(downloads, [])

    def test_rp_command_is_unknown_not_roleplay(self) -> None:
        bot = AutomationBot.__new__(AutomationBot)
        bot.config = SimpleNamespace(authorized_users=frozenset())
        bot.bot_username = "media_bot"
        bot.api = FakeTelegramAPI()
        downloads: list[str] = []
        bot.prepare_download_to_chat = MethodType(lambda _self, _message, url: downloads.append(url), bot)

        bot.handle_message(
            {
                "text": "/rp add share {actor} shares https://example.test",
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 100},
                "message_id": 10,
            }
        )

        self.assertEqual(downloads, [])
        self.assertEqual(bot.api.messages[-1][1], "Unknown command. Send a supported media-platform URL to download it.")

    def test_group_chat_detection(self) -> None:
        self.assertTrue(is_group_chat({"chat": {"type": "group"}}))
        self.assertTrue(is_group_chat({"chat": {"type": "supergroup"}}))
        self.assertFalse(is_group_chat({"chat": {"type": "private"}}))


class DownloadableUrlTests(unittest.TestCase):
    def test_detects_conservative_media_platform_urls(self) -> None:
        downloadable = [
            "https://www.youtube.com/watch?v=abc",
            "https://vm.tiktok.com/ZMh123/",
            "https://www.instagram.com/reel/abc123/",
            "https://x.com/example/status/123",
            "https://www.reddit.com/r/videos/comments/abc/title/",
            "https://www.twitch.tv/example/clip/CuriousClip",
            "https://vimeo.com/123456789",
            "https://soundcloud.com/artist/track",
        ]

        for url in downloadable:
            with self.subTest(url=url):
                self.assertTrue(is_downloadable_url(url))

    def test_rejects_ordinary_and_direct_file_urls(self) -> None:
        rejected = [
            "https://example.test/article",
            "https://example.test/video.mp4",
            "https://example.test/archive.zip?download=1",
            "https://cdn.example.test/photo.jpg",
            "https://example.test/document.pdf",
        ]

        for url in rejected:
            with self.subTest(url=url):
                self.assertFalse(is_downloadable_url(url))

    def test_first_download_url_skips_ordinary_links(self) -> None:
        self.assertEqual(
            first_download_url("docs https://example.test then https://x.com/example/status/123"),
            "https://x.com/example/status/123",
        )


class ChatContextTests(unittest.TestCase):
    def test_extracts_business_and_direct_message_context(self) -> None:
        context = ChatContext.from_message(
            {
                "chat": {"id": 123, "type": "private"},
                "message_id": 77,
                "business_connection_id": "biz-1",
                "direct_messages_topic": {"topic_id": 42},
            }
        )

        self.assertEqual(context.chat_id, 123)
        self.assertEqual(context.reply_to_message_id, 77)
        self.assertEqual(context.business_connection_id, "biz-1")
        self.assertEqual(context.direct_messages_topic_id, 42)

    def test_normal_download_passes_chat_context_to_send_calls(self) -> None:
        bot = AutomationBot.__new__(AutomationBot)
        bot.config = SimpleNamespace(api_base="https://api.telegram.org", max_upload_bytes=50 * 1024 * 1024)
        bot.api = FakeTelegramAPI()
        media_path = make_media_file(".mp4")
        bot.downloader = FakeDownloader(DownloadedMedia(paths=(media_path,), title="video title"))
        bot.sessions = {}

        bot.download_to_chat(
            {
                "chat": {"id": 123, "type": "private"},
                "message_id": 77,
                "from": {"id": 5},
                "business_connection_id": "biz-1",
                "direct_messages_topic_id": 42,
            },
            "https://example.test/video",
        )

        self.assertEqual(bot.api.messages[0][2].business_connection_id, "biz-1")
        self.assertEqual(bot.api.media[0][2].direct_messages_topic_id, 42)


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
                    [{"text": "Cancel", "callback_data": "dls:abc123:cancel"}],
                ]
            },
        )

    def test_session_keyboard_includes_retry_quality_and_cancel_for_youtube(self) -> None:
        session = DownloadSession(
            token="abc123",
            url="https://www.youtube.com/watch?v=abc",
            context=ChatContext(chat_id=1, requester_id=5),
            requester_id=5,
            selected_quality=QualityOption("h720", "720p", quality_format_selector(720)),
            created_at=time.time(),
        )

        self.assertEqual(
            session.action_keyboard(),
            {
                "inline_keyboard": [
                    [{"text": "Retry", "callback_data": "dls:abc123:retry"}],
                    [{"text": "Quality", "callback_data": "dls:abc123:quality"}],
                    [{"text": "Cancel", "callback_data": "dls:abc123:cancel"}],
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

    def test_quality_callback_downloads_selected_quality_and_keeps_session(self) -> None:
        bot = AutomationBot.__new__(AutomationBot)
        bot.sessions = {
            "token": DownloadSession(
                token="token",
                url="https://www.youtube.com/watch?v=abc",
                context=ChatContext(chat_id=-100, reply_to_message_id=10, requester_id=200),
                requester_id=200,
                created_at=time.time(),
                options={
                    "h720": QualityOption("h720", "720p ~80 MB", quality_format_selector(720)),
                },
            )
        }
        bot.api = FakeTelegramAPI()
        calls: list[tuple[str, int | None]] = []
        bot.run_download_session = MethodType(
            lambda _self, session, *, callback_message_id=None: calls.append(
                (session.selected_quality.format_selector, callback_message_id)
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

        self.assertEqual(calls, [(quality_format_selector(720), 99)])
        self.assertIn("token", bot.sessions)
        self.assertEqual(bot.sessions["token"].attempt_count, 0)
        self.assertEqual(bot.api.callback_answers, [("callback-id", "Downloading 720p ~80 MB...")])

    def test_retry_callback_reruns_same_url_and_format_and_increments_attempts(self) -> None:
        session = DownloadSession(
            token="token",
            url="https://www.youtube.com/watch?v=abc",
            context=ChatContext(chat_id=-100, reply_to_message_id=10, requester_id=200),
            requester_id=200,
            selected_quality=QualityOption("h720", "720p", quality_format_selector(720)),
            created_at=time.time(),
        )
        bot = AutomationBot.__new__(AutomationBot)
        bot.sessions = {"token": session}
        bot.api = FakeTelegramAPI()
        calls: list[tuple[str, str | None, int]] = []
        bot.run_download_session = MethodType(
            lambda _self, active, *, callback_message_id=None: calls.append(
                (active.url, active.selected_quality.format_selector, active.attempt_count)
            ),
            bot,
        )

        bot.handle_callback_query(
            {
                "id": "callback-id",
                "from": {"id": 200},
                "data": "dls:token:retry",
                "message": {"message_id": 99, "chat": {"id": -100}},
            }
        )

        self.assertEqual(calls, [("https://www.youtube.com/watch?v=abc", quality_format_selector(720), 1)])
        self.assertEqual(bot.api.callback_answers, [("callback-id", "Retrying...")])

    def test_quality_reselect_callback_recreates_options_for_original_url(self) -> None:
        session = DownloadSession(
            token="token",
            url="https://www.youtube.com/watch?v=abc",
            context=ChatContext(chat_id=-100, reply_to_message_id=10, requester_id=200),
            requester_id=200,
            selected_quality=QualityOption("h720", "720p", quality_format_selector(720)),
            created_at=time.time(),
        )
        bot = AutomationBot.__new__(AutomationBot)
        bot.sessions = {"token": session}
        bot.api = FakeTelegramAPI()
        bot.downloader = SimpleNamespace(
            youtube_quality_options=lambda _url: [QualityOption("auto", "Auto", None)]
        )

        bot.handle_callback_query(
            {
                "id": "callback-id",
                "from": {"id": 200},
                "data": "dls:token:quality",
                "message": {"message_id": 99, "chat": {"id": -100}},
            }
        )

        self.assertEqual(bot.api.callback_answers, [("callback-id", "Choose quality.")])
        self.assertEqual(bot.api.messages[-1][1], "Choose YouTube quality:")
        self.assertEqual(bot.sessions["token"].options["auto"].label, "Auto")

    def test_expired_and_foreign_callbacks_are_rejected(self) -> None:
        bot = AutomationBot.__new__(AutomationBot)
        bot.sessions = {
            "old": DownloadSession(
                token="old",
                url="https://example.test",
                context=ChatContext(chat_id=-100),
                requester_id=200,
                created_at=0,
            ),
            "foreign": DownloadSession(
                token="foreign",
                url="https://example.test",
                context=ChatContext(chat_id=-100),
                requester_id=200,
                created_at=time.time(),
            ),
        }
        bot.api = FakeTelegramAPI()

        bot.handle_callback_query({"id": "old-cb", "from": {"id": 200}, "data": "dls:old:retry"})
        bot.handle_callback_query({"id": "foreign-cb", "from": {"id": 201}, "data": "dls:foreign:retry"})

        self.assertEqual(
            bot.api.callback_answers,
            [
                ("old-cb", "This download expired."),
                ("foreign-cb", "Only the requester can use this download."),
            ],
        )


class AlbumFallbackTests(unittest.TestCase):
    def test_album_chunk_upload_retries_once_then_sends_photos_individually(self) -> None:
        bot = AutomationBot.__new__(AutomationBot)
        bot.config = SimpleNamespace(api_base="https://api.telegram.org", max_upload_bytes=50 * 1024 * 1024)
        bot.api = FakeTelegramAPI(fail_album_calls=2)
        paths = tuple(make_media_file(".jpg") for _index in range(3))
        media = DownloadedMedia(paths=paths, title="album title")

        bot.send_downloaded_media(ChatContext(chat_id=-100, reply_to_message_id=9), media)

        self.assertEqual(len(bot.api.album_attempts), 2)
        self.assertEqual([call[1].name for call in bot.api.media], [path.name for path in paths])
        self.assertEqual(bot.api.messages[-1][1], "Album upload failed; sent photos one by one.")


class ErrorMessageTests(unittest.TestCase):
    def test_maps_common_failures_to_short_user_text(self) -> None:
        cases = {
            RuntimeError("file is 51.0 MB, max upload is 50 MB"): "The file is too large for Telegram upload.",
            RuntimeError("ERROR: [youtube] Private video"): "Media is unavailable or private.",
            RuntimeError("yt-dlp completed without a usable output file"): "Could not find downloadable media metadata.",
            TelegramAPIError("sendVideo failed: Request Entity Too Large"): "Telegram upload failed.",
            TimeoutError("timed out"): "Network timeout while downloading media.",
        }

        for exc, expected in cases.items():
            with self.subTest(exc=exc):
                self.assertEqual(friendly_error_message(exc), expected)


class FakeDownloader:
    def __init__(self, media: DownloadedMedia) -> None:
        self.media = media

    def download(self, _url: str, *, format_selector: str | None = None) -> "FakeDownloader":
        self.format_selector = format_selector
        return self

    def __enter__(self) -> DownloadedMedia:
        return self.media

    def __exit__(self, *_args: object) -> None:
        return None


class FakeTelegramAPI:
    def __init__(self, *, fail_album_calls: int = 0) -> None:
        self.fail_album_calls = fail_album_calls
        self.callback_answers: list[tuple[str, str]] = []
        self.edits: list[tuple[int, int, str, object]] = []
        self.deletions: list[tuple[int, int]] = []
        self.messages: list[tuple[int, str, ChatContext, object]] = []
        self.media: list[tuple[int, Path, ChatContext]] = []
        self.album_attempts: list[tuple[int, tuple[Path, ...], ChatContext]] = []
        self.next_message_id = 100

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> bool:
        self.callback_answers.append((callback_query_id, text))
        return True

    def send_message(
        self,
        context: ChatContext,
        text: str,
        *,
        reply_markup: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.messages.append((context.chat_id, text, context, reply_markup))
        self.next_message_id += 1
        return {"message_id": self.next_message_id}

    def edit_message_text(
        self,
        context: ChatContext,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, object] | None = None,
    ) -> bool:
        self.edits.append((context.chat_id, message_id, text, reply_markup))
        return True

    def delete_message(self, context: ChatContext, message_id: int) -> bool:
        self.deletions.append((context.chat_id, message_id))
        return True

    def send_media(self, context: ChatContext, file_path: Path, **_kwargs: object) -> dict[str, object]:
        self.media.append((context.chat_id, file_path, context))
        return {"document": {"file_id": "file-id"}}

    def send_photo_album(self, context: ChatContext, file_paths: tuple[Path, ...], **_kwargs: object) -> list[dict[str, object]]:
        self.album_attempts.append((context.chat_id, file_paths, context))
        if self.fail_album_calls:
            self.fail_album_calls -= 1
            raise TelegramAPIError("sendMediaGroup failed: upload failed")
        return [{"photo": [{"file_id": "file-id"}]}]


def make_media_file(suffix: str) -> Path:
    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        handle.write(b"data")
        return Path(handle.name)
    finally:
        handle.close()


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
