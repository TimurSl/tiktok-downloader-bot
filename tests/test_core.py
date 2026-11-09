import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

from tiktok_downloader_bot.captions import build_caption, clean_title
from tiktok_downloader_bot.bot import (
    ProgressReporter,
    format_progress_detail,
    option_label,
    quality_keyboard,
)
from tiktok_downloader_bot.config import (
    PUBLIC_BOT_API_UPLOAD_LIMIT_BYTES,
    effective_upload_limit_bytes,
    parse_user_ids,
)
from tiktok_downloader_bot.downloader import (
    DownloadedMedia,
    MediaDownloader,
    ProgressInfo,
    QualityOption,
    convert_to_animation,
    probe_media,
    best_audio_format,
    best_video_format,
    combined_format_size,
    first_entry,
    quality_format_selector,
)
from tiktok_downloader_bot.errors import (
    NoSoundError,
    TooLargeError,
    error_key,
    friendly_error_message,
)
from tiktok_downloader_bot.i18n import STRINGS, normalize_lang, tr
from tiktok_downloader_bot.media import (
    album_chunks,
    chunk_paths,
    find_downloaded_files,
    photo_album_chunks,
    select_downloaded_media,
)
from tiktok_downloader_bot.scheduler import JobScheduler
from tiktok_downloader_bot.storage import FileIdCache, SettingsStore
from tiktok_downloader_bot.telegram_api import (
    ChatContext,
    TelegramAPI,
    TelegramAPIError,
    stringify_payload,
)
from tiktok_downloader_bot.tiktok import (
    extract_tiktok_image_urls,
    extract_tiktok_item_struct,
    extract_tiktok_music,
    extract_tiktok_rehydration_data,
)
from tiktok_downloader_bot.twitter import (
    clean_tweet_text,
    is_no_video_error,
    is_twitter_url,
    normalize_twitter_url,
    original_photo_url,
    parse_fxtwitter,
    parse_syndication,
    syndication_token,
    twitter_screen_name,
    twitter_status_id,
)
from tiktok_downloader_bot.urls import (
    cache_url,
    download_urls,
    first_download_url,
    is_downloadable_url,
    is_tiktok_photo_url,
    is_tiktok_redirect_url,
    is_tiktok_url,
    is_youtube_url,
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

    def test_mixed_photo_and_video_carousel_is_kept_as_one_album(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for name in ["post-1.jpg", "post-2.mp4", "post-3.jpg"]:
                (directory / name).write_bytes(b"data" * 3)

            paths = select_downloaded_media(find_downloaded_files(directory))
            media = DownloadedMedia(paths=paths, title="carousel")

            self.assertEqual([path.name for path in paths], ["post-1.jpg", "post-2.mp4", "post-3.jpg"])
            self.assertEqual(media.kinds, ("photo", "video", "photo"))
            self.assertTrue(media.is_album)

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

    def test_album_chunk_sizes_for_telegram_album_limits(self) -> None:
        cases = {
            2: [2],
            9: [9],
            10: [10],
            11: [9, 2],
            23: [10, 10, 3],
        }

        for count, expected_sizes in cases.items():
            with self.subTest(count=count):
                paths = tuple(Path(f"image-{index}.jpg") for index in range(count))
                self.assertEqual([len(chunk) for chunk in album_chunks(paths)], expected_sizes)
                self.assertEqual(album_chunks(paths), photo_album_chunks(paths))


class DownloaderStrategyTests(unittest.TestCase):
    def make_downloader(self, ytdlp_format: str = "bv*+ba/best") -> MediaDownloader:
        downloader = MediaDownloader.__new__(MediaDownloader)
        downloader.config = SimpleNamespace(ytdlp_format=ytdlp_format)
        return downloader

    def test_download_strategies_try_requested_then_fallback_formats(self) -> None:
        strategies = self.make_downloader().download_strategies("bv*[height<=720]+ba/b[height<=720]/best")

        self.assertEqual(strategies[0].name, "requested")
        self.assertEqual(strategies[0].format_selector, "bv*[height<=720]+ba/b[height<=720]/best")
        self.assertIn("bestvideo*+bestaudio/bestvideo+bestaudio/best", [s.format_selector for s in strategies])
        self.assertIn("best", [s.format_selector for s in strategies])

    def test_download_strategies_dedupe_configured_default(self) -> None:
        strategies = self.make_downloader("best").download_strategies(None)

        self.assertEqual([strategy.format_selector for strategy in strategies].count("best"), 1)

    def test_fallback_strategies_never_exceed_the_chosen_height(self) -> None:
        strategies = self.make_downloader().download_strategies(quality_format_selector(360), max_height=360)

        selectors = [strategy.format_selector for strategy in strategies]
        self.assertTrue(all("height<=360" in selector for selector in selectors), selectors)
        self.assertFalse(any(selector in {"best", "bestvideo*+bestaudio/bestvideo+bestaudio/best"} for selector in selectors))

    def test_audio_strategies(self) -> None:
        strategies = self.make_downloader().download_strategies(None, audio_only=True)

        self.assertEqual([s.format_selector for s in strategies], ["bestaudio/best", "best"])

    def test_ytdlp_options_extract_mp3_for_audio_and_merge_mp4_for_video(self) -> None:
        downloader = self.make_downloader()
        downloader.config = SimpleNamespace(
            ytdlp_no_playlist=True, ytdlp_cookies=None, ytdlp_cookies_from_browser=None
        )

        audio = downloader.ytdlp_options(Path("/tmp/x"), "bestaudio/best", audio_only=True)
        video = downloader.ytdlp_options(Path("/tmp/x"), "best")

        self.assertEqual(audio["postprocessors"][0]["key"], "FFmpegExtractAudio")
        self.assertNotIn("merge_output_format", audio)
        self.assertEqual(video["merge_output_format"], "mp4")
        self.assertNotIn("postprocessors", video)

    def test_first_entry_prefers_playlist_item(self) -> None:
        self.assertEqual(first_entry({"entries": [None, {"title": "a"}]}), {"title": "a"})
        self.assertEqual(first_entry({"title": "single"}), {"title": "single"})
        self.assertEqual(first_entry(None), {})


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


class TikTokMusicTests(unittest.TestCase):
    def test_extracts_music_url_title_and_author(self) -> None:
        item = {
            "author": {"nickname": "Bob"},
            "music": {"playUrl": "https://example.test/music.mp3", "title": "original sound", "authorName": "Alice"},
        }

        music = extract_tiktok_music(item)

        self.assertEqual((music.url, music.title, music.author), ("https://example.test/music.mp3", "original sound", "Alice"))

    def test_music_falls_back_to_post_author_and_handles_missing_music(self) -> None:
        item = {"author": {"nickname": "Bob"}, "music": {"play_url": {"url_list": ["https://example.test/m.mp3"]}}}

        self.assertEqual(extract_tiktok_music(item).author, "Bob")
        self.assertIsNone(extract_tiktok_music({"music": {"title": "no url"}}))
        self.assertIsNone(extract_tiktok_music({}))


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


class UrlTests(unittest.TestCase):
    def test_download_urls_returns_distinct_supported_links_in_order(self) -> None:
        text = (
            "see https://example.test/page then https://vm.tiktok.com/ZM1/ and "
            "https://www.youtube.com/watch?v=abc&si=track plus https://www.youtube.com/watch?v=abc again "
            "and https://youtu.be/xyz"
        )

        self.assertEqual(
            download_urls(text),
            ["https://vm.tiktok.com/ZM1/", "https://www.youtube.com/watch?v=abc&si=track", "https://youtu.be/xyz"],
        )
        self.assertEqual(download_urls(text, limit=2), download_urls(text)[:2])

    def test_cache_url_ignores_tracking_parameters(self) -> None:
        self.assertEqual(
            cache_url("https://www.youtube.com/watch?v=abc&si=track&utm_source=x#t=3"),
            cache_url("https://WWW.youtube.com/watch?v=abc"),
        )
        self.assertEqual(
            cache_url("https://www.tiktok.com/@a/video/1?_r=1&_t=zz"),
            cache_url("https://www.tiktok.com/@a/video/1"),
        )
        self.assertNotEqual(cache_url("https://youtu.be/a"), cache_url("https://youtu.be/b"))


class CaptionTests(unittest.TestCase):
    def test_caption_has_description_uploader_and_source_link(self) -> None:
        caption, parse_mode = build_caption("hello #fyp #viral", "Bob", "https://www.tiktok.com/@bob/video/1")

        self.assertEqual(parse_mode, "HTML")
        self.assertEqual(caption, 'hello\n\nBob · <a href="https://www.tiktok.com/@bob/video/1">tiktok.com</a>')

    def test_trailing_hashtags_are_trimmed_but_not_when_they_are_the_whole_title(self) -> None:
        self.assertEqual(clean_title("a  b\n#x #y"), "a b")
        self.assertEqual(clean_title("#only #tags"), "#only #tags")
        self.assertEqual(clean_title("keep #inline tag here"), "keep #inline tag here")

    def test_caption_is_html_escaped_and_fits_telegram_limit(self) -> None:
        caption, _mode = build_caption("<b>" + "&" * 3000, "A&B", "https://example.test/?a=1&b=2")

        self.assertLessEqual(len(caption), 1024)
        self.assertIn("&lt;b&gt;", caption)
        self.assertIn("A&amp;B", caption)
        self.assertIn('href="https://example.test/?a=1&amp;b=2"', caption)
        self.assertIn("<blockquote expandable>", caption)
        self.assertTrue(caption.count("<blockquote expandable>") == caption.count("</blockquote>") == 1)

    def test_disabled_captions_and_untitled_media(self) -> None:
        self.assertEqual(build_caption("title", "Bob", "https://x.test/", enabled=False), ("", None))
        caption, mode = build_caption("", "", "https://www.example.test/v")
        self.assertEqual((caption, mode), ('<a href="https://www.example.test/v">example.test</a>', "HTML"))


class ErrorMessageTests(unittest.TestCase):
    def test_maps_common_failures_to_short_user_text(self) -> None:
        cases = {
            RuntimeError("file is 51.0 MB, max upload is 50 MB"): "The file is too large for Telegram upload.",
            TooLargeError("whatever"): "The file is too large for Telegram upload.",
            RuntimeError("ERROR: [youtube] Private video"): "Media is unavailable or private.",
            RuntimeError("yt-dlp completed without a usable output file"): "Could not find downloadable media metadata.",
            RuntimeError("TikTok photo post did not expose image URLs"): "Could not find downloadable media metadata.",
            TelegramAPIError("sendVideo failed: Bad Request"): "Telegram upload failed.",
            TelegramAPIError("sendVideo failed: Request Entity Too Large"): "The file is too large for Telegram upload.",
            TimeoutError("timed out"): "Network timeout while downloading media.",
            RuntimeError("something odd about metadata"): "Download failed.",
            NoSoundError("x"): "This post has no downloadable sound.",
        }

        for exc, expected in cases.items():
            with self.subTest(exc=exc):
                self.assertEqual(friendly_error_message(exc), expected)

    def test_specific_causes_get_their_own_message(self) -> None:
        cases = {
            "ERROR: [youtube] abc: Sign in to confirm you\u2019re not a bot": "bot_check",
            "Sign in to confirm you're not a bot": "bot_check",
            "This video is age-restricted": "age",
            "The uploader has not made this video available in your country": "geo",
            "HTTP Error 429: Too Many Requests": "rate_limit",
            "Unsupported URL: https://example.test": "unsupported",
        }
        for text, key in cases.items():
            with self.subTest(text=text):
                self.assertEqual(error_key(RuntimeError(text)), key)

    def test_messages_are_localized(self) -> None:
        self.assertEqual(friendly_error_message(RuntimeError("Private video"), "ru"), STRINGS["ru"]["err_unavailable"])
        self.assertEqual(friendly_error_message(RuntimeError("Private video"), "xx"), STRINGS["en"]["err_unavailable"])


class I18nTests(unittest.TestCase):
    def test_all_languages_define_the_same_keys_and_placeholders(self) -> None:
        import string

        def fields(text: str) -> set[str]:
            return {name for _, name, _, _ in string.Formatter().parse(text) if name}

        english = STRINGS["en"]
        for lang, table in STRINGS.items():
            with self.subTest(lang=lang):
                self.assertEqual(set(table), set(english))
                for key, text in table.items():
                    self.assertEqual(fields(text), fields(english[key]), key)

    def test_normalize_lang(self) -> None:
        self.assertEqual(normalize_lang("ru-RU"), "ru")
        self.assertEqual(normalize_lang("uk"), "uk")
        self.assertEqual(normalize_lang("ua"), "uk")
        self.assertEqual(normalize_lang("pt_BR"), "en")
        self.assertEqual(normalize_lang(None), "en")
        self.assertEqual(tr("ru", "cb_downloading_quality", label="720p"), "Скачиваю 720p…")


class StorageTests(unittest.TestCase):
    def test_settings_defaults_toggle_and_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            store = SettingsStore(path)
            self.assertEqual(store.get(-100)["quality"], "ask")
            self.assertTrue(store.get(-100)["captions"])
            self.assertFalse(store.get(-100)["reply"])

            self.assertFalse(store.toggle(-100, "captions")["captions"])
            self.assertEqual(store.toggle(-100, "quality")["quality"], "auto")
            with self.assertRaises(KeyError):
                store.toggle(-100, "nope")

            reloaded = SettingsStore(path)
            self.assertFalse(reloaded.get(-100)["captions"])
            self.assertEqual(reloaded.get(-100)["quality"], "auto")
            self.assertTrue(reloaded.get(5)["captions"])

    def test_quality_cycles_back_to_ask(self) -> None:
        store = SettingsStore(None)
        seen = [store.toggle(1, "quality")["quality"] for _ in range(6)]

        self.assertEqual(seen, ["auto", "1080", "720", "480", "audio", "ask"])

    def test_settings_ignore_corrupt_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"1": {"quality": "bogus", "captions": "yes"}}), encoding="utf-8")

            values = SettingsStore(path).get(1)

            self.assertEqual(values["quality"], "ask")
            self.assertTrue(values["captions"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(SettingsStore(path).get(1)["quality"], "ask")

    def test_file_id_cache_is_lru_and_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            cache = FileIdCache(path, max_entries=2)
            cache.put("a", {"items": [["video", "1"]]})
            cache.put("b", {"items": [["video", "2"]]})
            self.assertIsNotNone(cache.get("a"))  # touching a makes b the oldest
            cache.put("c", {"items": [["video", "3"]]})

            self.assertIsNone(cache.get("b"))
            reloaded = FileIdCache(path, max_entries=2)
            self.assertEqual(reloaded.get("c"), {"items": [["video", "3"]]})
            reloaded.discard("c")
            self.assertEqual(len(FileIdCache(path)), 1)


class SchedulerTests(unittest.TestCase):
    def test_per_user_limit_does_not_block_other_users(self) -> None:
        scheduler = JobScheduler(max_workers=3, per_key_limit=1, max_pending_per_key=5)
        release = threading.Event()
        started: list[str] = []
        lock = threading.Lock()

        def job(name: str):
            def run() -> None:
                with lock:
                    started.append(name)
                release.wait(5)

            return run

        try:
            self.assertTrue(scheduler.submit("alice", job("a1")))
            self.assertTrue(scheduler.submit("alice", job("a2")))
            self.assertTrue(scheduler.submit("bob", job("b1")))
            deadline = time.time() + 5
            while len(started) < 2 and time.time() < deadline:
                time.sleep(0.01)

            self.assertEqual(sorted(started), ["a1", "b1"])  # a2 waits for a1, bob is not blocked
            self.assertEqual(scheduler.counts(), (2, 1))

            release.set()
            deadline = time.time() + 5
            while len(started) < 3 and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(sorted(started), ["a1", "a2", "b1"])
        finally:
            release.set()
            scheduler.shutdown()

    def test_rejects_jobs_beyond_the_pending_cap_and_survives_job_errors(self) -> None:
        scheduler = JobScheduler(max_workers=1, per_key_limit=1, max_pending_per_key=2)
        release = threading.Event()
        try:
            self.assertTrue(scheduler.submit("u", lambda: release.wait(5)))
            self.assertTrue(scheduler.submit("u", lambda: (_ for _ in ()).throw(RuntimeError("boom"))))
            self.assertFalse(scheduler.submit("u", lambda: None))
            release.set()
            done = threading.Event()
            deadline = time.time() + 5
            while scheduler.counts() != (0, 0) and time.time() < deadline:
                time.sleep(0.01)
            self.assertTrue(scheduler.submit("u", done.set))
            self.assertTrue(done.wait(5))
        finally:
            release.set()
            scheduler.shutdown()


class ProgressReporterTests(unittest.TestCase):
    def test_edits_are_throttled_deduplicated_and_back_off_on_flood_limits(self) -> None:
        now = [0.0]
        edits: list[str] = []
        fail = [False]

        def edit(text: str) -> None:
            if fail[0]:
                raise TelegramAPIError("editMessageText failed: Too Many Requests", retry_after=10)
            edits.append(text)

        reporter = ProgressReporter(edit, "en", interval=3.0, clock=lambda: now[0])
        reporter(ProgressInfo("downloading", 10.0, 2 * 1024 * 1024))
        reporter(ProgressInfo("downloading", 20.0, 2 * 1024 * 1024))  # too soon
        now[0] = 3.5
        reporter(ProgressInfo("downloading", 40.0, 512 * 1024))
        reporter(ProgressInfo("downloading", 40.0, 512 * 1024))  # same text
        now[0] = 7.0
        fail[0] = True
        reporter(ProgressInfo("downloading", 70.0, None))  # flood limited -> back off
        fail[0] = False
        now[0] = 12.0
        reporter(ProgressInfo("downloading", 80.0, None))  # still inside the 10 s back-off
        now[0] = 18.0
        reporter(ProgressInfo("downloading", 90.0, None))
        reporter(ProgressInfo("processing"))

        self.assertEqual(
            edits,
            ["Downloading… 10% · 2.0 MB/s", "Downloading… 40% · 512 KB/s", "Downloading… 90%", "Processing…"],
        )

    def test_progress_detail_formatting(self) -> None:
        self.assertEqual(format_progress_detail(ProgressInfo("downloading", 150.0, None)), "100%")
        self.assertEqual(format_progress_detail(ProgressInfo("downloading", None, None)), "")


class QualityOptionTests(unittest.TestCase):
    def test_detects_youtube_urls(self) -> None:
        self.assertTrue(is_youtube_url("https://www.youtube.com/watch?v=abc"))
        self.assertTrue(is_youtube_url("https://youtu.be/abc"))
        self.assertFalse(is_youtube_url("https://example.test/watch?v=abc"))

    def test_quality_keyboard_uses_callback_data_and_localized_special_labels(self) -> None:
        options = [
            QualityOption("auto", "Auto", None, max_height=720),
            QualityOption("h720", "720p ~80 MB", quality_format_selector(720), max_height=720),
            QualityOption("audio", "Audio (MP3)", "bestaudio/best", audio_only=True),
        ]

        self.assertEqual(
            quality_keyboard("abc123", options),
            {
                "inline_keyboard": [
                    [{"text": "Auto (best that fits) (720p)", "callback_data": "dlq:abc123:auto"}],
                    [{"text": "720p ~80 MB", "callback_data": "dlq:abc123:h720"}],
                    [{"text": "🎵 Audio only (MP3)", "callback_data": "dlq:abc123:audio"}],
                    [{"text": "Cancel", "callback_data": "dls:abc123:cancel"}],
                ]
            },
        )
        self.assertEqual(option_label(options[2], "ru"), STRINGS["ru"]["quality_audio"])
        self.assertEqual(quality_keyboard("t", options, "ru")["inline_keyboard"][-1][0]["text"], "Отмена")

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

    def test_youtube_options_hide_oversized_qualities_and_auto_picks_best_that_fits(self) -> None:
        downloader = MediaDownloader.__new__(MediaDownloader)
        downloader.config = SimpleNamespace(
            download_dir=Path(tempfile.gettempdir()),
            ytdlp_no_playlist=True,
            ytdlp_cookies=None,
            ytdlp_cookies_from_browser=None,
            api_base="https://api.telegram.org",
            max_upload_bytes=50 * 1024 * 1024,
        )
        mb = 1024 * 1024
        info = {
            "duration": 100,
            "formats": [
                {"height": 1080, "vcodec": "avc1", "acodec": "none", "filesize": 300 * mb},
                {"height": 720, "vcodec": "avc1", "acodec": "none", "filesize": 120 * mb},
                {"height": 480, "vcodec": "avc1", "acodec": "none", "filesize": 30 * mb},
                {"height": 360, "vcodec": "avc1", "acodec": "none", "filesize": 12 * mb},
                {"vcodec": "none", "acodec": "mp4a", "filesize": 2 * mb, "abr": 128},
            ],
        }

        class FakeYDL:
            def __init__(self, _options):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def extract_info(self, _url, download=False):
                return info

        import tiktok_downloader_bot.downloader as module

        original = module.yt_dlp.YoutubeDL
        module.yt_dlp.YoutubeDL = FakeYDL
        try:
            options = downloader.youtube_quality_options("https://youtu.be/abc")
        finally:
            module.yt_dlp.YoutubeDL = original

        self.assertEqual([option.key for option in options], ["auto", "h480", "h360", "audio"])
        self.assertEqual(options[0].format_selector, quality_format_selector(480))
        self.assertEqual(options[0].max_height, 480)
        self.assertTrue(options[-1].audio_only)

        info["formats"] = [f for f in info["formats"] if f.get("height") in {1080, 720} or f.get("acodec") == "mp4a"]
        module.yt_dlp.YoutubeDL = FakeYDL
        try:
            options = downloader.youtube_quality_options("https://youtu.be/abc")
        finally:
            module.yt_dlp.YoutubeDL = original
        # Nothing fits: still offer the smallest one, flagged with a warning sign.
        self.assertEqual([option.key for option in options], ["auto", "h720", "audio"])
        self.assertTrue(options[1].label.endswith("⚠"))


class UploadLimitTests(unittest.TestCase):
    def test_public_bot_api_clamps_configured_limit_to_telegram_upload_cap(self) -> None:
        config = SimpleNamespace(api_base="https://api.telegram.org", max_upload_bytes=100 * 1024 * 1024)

        self.assertEqual(effective_upload_limit_bytes(config), PUBLIC_BOT_API_UPLOAD_LIMIT_BYTES)

    def test_custom_bot_api_keeps_configured_limit(self) -> None:
        config = SimpleNamespace(api_base="http://127.0.0.1:8081", max_upload_bytes=100 * 1024 * 1024)

        self.assertEqual(effective_upload_limit_bytes(config), 100 * 1024 * 1024)

    def test_parse_user_ids(self) -> None:
        self.assertEqual(parse_user_ids(" 1, 2,,3 "), frozenset({1, 2, 3}))
        self.assertEqual(parse_user_ids(""), frozenset())


class TelegramPayloadTests(unittest.TestCase):
    def test_messages_are_silent_and_do_not_reply_by_default(self) -> None:
        context = ChatContext.from_message({"chat": {"id": 123, "type": "private"}, "message_id": 77})

        payload = context.send_payload()

        self.assertTrue(payload["disable_notification"])
        self.assertNotIn("reply_parameters", payload)
        self.assertEqual(context.source_message_id, 77)
        self.assertIsNone(context.reply_to_message_id)

    def test_optional_replies_are_still_silent(self) -> None:
        context = ChatContext.from_message({"chat": {"id": 123, "type": "group"}, "message_id": 77}, reply=True)

        payload = context.send_payload()

        self.assertEqual(payload["reply_parameters"]["message_id"], 77)
        self.assertTrue(payload["disable_notification"])

    def test_non_message_calls_do_not_carry_notification_or_reply_fields(self) -> None:
        context = ChatContext(chat_id=1, reply_to_message_id=5, business_connection_id="b")

        self.assertEqual(context.base_payload(), {"chat_id": 1, "business_connection_id": "b"})

    def test_extracts_business_direct_message_language_and_chat_type_context(self) -> None:
        context = ChatContext.from_message(
            {
                "chat": {"id": 123, "type": "supergroup"},
                "message_id": 77,
                "from": {"id": 9, "language_code": "uk-UA"},
                "business_connection_id": "biz-1",
                "direct_messages_topic": {"topic_id": 42},
            }
        )

        self.assertEqual(context.chat_id, 123)
        self.assertEqual(context.requester_id, 9)
        self.assertEqual(context.business_connection_id, "biz-1")
        self.assertEqual(context.direct_messages_topic_id, 42)
        self.assertEqual((context.lang, context.chat_type), ("uk", "supergroup"))

    def test_stringify_payload_uses_json_and_lowercase_booleans(self) -> None:
        self.assertEqual(
            stringify_payload({"a": True, "b": {"x": 1}, "c": 5, "d": False}),
            {"a": "true", "b": '{"x": 1}', "c": "5", "d": "false"},
        )

    def test_media_group_validates_size(self) -> None:
        api = TelegramAPI(SimpleNamespace(api_base="http://x", token="t", poll_timeout=1))
        with self.assertRaises(ValueError):
            api.send_media_group(ChatContext(chat_id=1), [("photo", "a")])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg is required")
class GifConversionTests(unittest.TestCase):
    def make_source(self, directory: Path, *, size: str = "853x480", seconds: int = 3) -> Path:
        # MJPEG in Matroska so that odd frame sizes (which H.264 refuses) can be generated too.
        source = directory / "source.mkv"
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size={size}:rate=30",
                "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:v", "mjpeg", "-pix_fmt", "yuvj420p",
                "-c:a", "aac", "-shortest", str(source),
            ],
            check=True,
        )
        return source

    def streams(self, path: Path) -> list[str]:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
            check=True, capture_output=True, text=True,
        ).stdout
        return out.split()

    def test_output_is_silent_small_and_has_even_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = self.make_source(Path(tmp), size="853x480")

            output = convert_to_animation(source, Path(tmp) / "gif")

            self.assertEqual(self.streams(source), ["video", "audio"])
            self.assertEqual(self.streams(output), ["video"])  # no sound track
            info = probe_media(output)
            self.assertLessEqual(info["width"], 480)
            self.assertEqual((info["width"] % 2, info["height"] % 2), (0, 0))
            self.assertEqual(info["duration"], 3)

    def test_small_videos_are_not_upscaled_and_odd_sizes_are_made_even(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = self.make_source(Path(tmp), size="321x241", seconds=1)

            output = convert_to_animation(source, Path(tmp) / "gif")

            info = probe_media(output)
            self.assertEqual((info["width"], info["height"]), (320, 240))

    def test_long_videos_are_cut_to_the_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = self.make_source(Path(tmp), size="160x90", seconds=5)

            output = convert_to_animation(source, Path(tmp) / "gif", max_seconds=2)

            self.assertEqual(probe_media(output)["duration"], 2)

    def test_unreadable_input_raises_a_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.mp4"
            broken.write_bytes(b"not a video")

            with self.assertRaisesRegex(RuntimeError, "could not convert"):
                convert_to_animation(broken, Path(tmp) / "gif")


SYNDICATION_PHOTOS = {
    "id_str": "1788888888888888888",
    "text": "two photos &amp; a caption https://t.co/AbCdEf123",
    "user": {"name": "Jack", "screen_name": "jack"},
    "mediaDetails": [
        {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/AAA.jpg"},
        {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/BBB.png"},
    ],
    "photos": [{"url": "https://pbs.twimg.com/media/AAA.jpg"}, {"url": "https://pbs.twimg.com/media/BBB.png"}],
}
FXTWITTER_PHOTOS = {
    "code": 200,
    "tweet": {
        "text": "from fx",
        "author": {"name": "Jack", "screen_name": "jack"},
        "media": {"photos": [{"type": "photo", "url": "https://pbs.twimg.com/media/CCC.jpg"}]},
    },
}


class TwitterParsingTests(unittest.TestCase):
    def test_status_urls_and_mirrors(self) -> None:
        self.assertEqual(twitter_status_id("https://x.com/jack/status/20/photo/1?s=20"), "20")
        self.assertEqual(twitter_status_id("https://twitter.com/i/web/status/99"), "99")
        self.assertEqual(twitter_status_id("https://mobile.twitter.com/jack/statuses/7"), "7")
        self.assertIsNone(twitter_status_id("https://x.com/jack"))
        self.assertEqual(twitter_screen_name("https://x.com/jack/status/20"), "jack")
        self.assertIsNone(twitter_screen_name("https://x.com/i/status/20"))
        self.assertTrue(all(is_twitter_url(u) for u in ("https://x.com/a/status/1", "https://www.twitter.com/a/status/1", "https://fxtwitter.com/a/status/1")))
        self.assertFalse(is_twitter_url("https://example.test/a/status/1"))

    def test_link_preview_mirrors_are_pointed_back_at_x(self) -> None:
        self.assertEqual(normalize_twitter_url("https://fxtwitter.com/jack/status/20?s=1"), "https://x.com/jack/status/20")
        self.assertEqual(normalize_twitter_url("https://www.vxtwitter.com/jack/status/20"), "https://x.com/jack/status/20")
        self.assertEqual(normalize_twitter_url("https://twitter.com/jack/status/20?s=1"), "https://twitter.com/jack/status/20?s=1")
        self.assertEqual(download_urls("https://fixupx.com/jack/status/20"), ["https://fixupx.com/jack/status/20"])

    def test_syndication_token_is_base36_without_zeros_or_dots(self) -> None:
        for twid in ("20", "1788888888888888888", "1999999999999999999"):
            token = syndication_token(twid)
            self.assertTrue(token and token.isalnum() and "0" not in token, token)

    def test_parses_photos_text_and_author_from_syndication(self) -> None:
        post = parse_syndication(SYNDICATION_PHOTOS)

        self.assertEqual(post.photo_urls, ("https://pbs.twimg.com/media/AAA.jpg", "https://pbs.twimg.com/media/BBB.png"))
        self.assertEqual((post.text, post.author), ("two photos & a caption", "Jack"))

    def test_syndication_ignores_videos_tombstones_and_garbage(self) -> None:
        video_only = {"text": "v", "mediaDetails": [{"type": "video", "media_url_https": "https://pbs.twimg.com/amplify/x.jpg"}]}
        self.assertEqual(parse_syndication(video_only).photo_urls, ())
        self.assertIsNone(parse_syndication({"__typename": "TweetTombstone"}))
        self.assertIsNone(parse_syndication({}))
        self.assertIsNone(parse_syndication("nope"))
        only_photos_key = {"text": "t", "photos": [{"url": "https://pbs.twimg.com/media/Z.jpg"}]}
        self.assertEqual(parse_syndication(only_photos_key).photo_urls, ("https://pbs.twimg.com/media/Z.jpg",))

    def test_parses_fxtwitter_answers(self) -> None:
        post = parse_fxtwitter(FXTWITTER_PHOTOS)
        self.assertEqual((post.photo_urls, post.text, post.author), (("https://pbs.twimg.com/media/CCC.jpg",), "from fx", "Jack"))

        via_all = {"tweet": {"text": "t", "media": {"all": [{"type": "video", "url": "v"}, {"type": "photo", "url": "https://pbs.twimg.com/media/D.jpg"}]}}}
        self.assertEqual(parse_fxtwitter(via_all).photo_urls, ("https://pbs.twimg.com/media/D.jpg",))
        self.assertIsNone(parse_fxtwitter({"code": 404}))

    def test_text_and_photo_url_helpers(self) -> None:
        self.assertEqual(clean_tweet_text("a &lt;b&gt; https://t.co/x1 https://t.co/y2"), "a <b>")
        self.assertEqual(clean_tweet_text("see https://t.co/x1 later"), "see https://t.co/x1 later")
        self.assertEqual(original_photo_url("https://pbs.twimg.com/media/AAA.jpg"), "https://pbs.twimg.com/media/AAA.jpg?name=orig")
        self.assertEqual(original_photo_url("https://pbs.twimg.com/media/AAA.jpg?name=small", size="large"), "https://pbs.twimg.com/media/AAA.jpg?name=large")
        self.assertEqual(original_photo_url("https://example.test/a.jpg"), "https://example.test/a.jpg")
        self.assertTrue(is_no_video_error(RuntimeError("ERROR: [twitter:1] 1: No video could be found in this tweet")))
        self.assertFalse(is_no_video_error(RuntimeError("HTTP Error 500")))

    def test_tweets_without_video_are_reported_as_missing_media(self) -> None:
        exc = RuntimeError("ERROR: [twitter] 1: No video could be found in this tweet")
        self.assertEqual(friendly_error_message(exc), "Could not find downloadable media metadata.")
        self.assertEqual(friendly_error_message(RuntimeError("Tweet did not expose any photos")), "Could not find downloadable media metadata.")


class FakeResponse:
    def __init__(self, *, json_data=None, content: bytes = b"", headers=None, status: int = 200) -> None:
        self._json = json_data
        self.content = content
        self.headers = headers or {}
        self.status = status

    def raise_for_status(self) -> None:
        if self.status >= 400:
            import requests

            raise requests.HTTPError(f"HTTP Error {self.status}")

    def json(self):
        return self._json

    def iter_content(self, chunk_size: int = 1):
        yield self.content


class FakeSession:
    """Answers by URL prefix; records what was requested."""

    def __init__(self, routes) -> None:
        self.routes = routes
        self.requests: list[str] = []

    def get(self, url, params=None, headers=None, stream=False, timeout=None):
        self.requests.append(url if not params else f"{url}?{'&'.join(f'{k}={v}' for k, v in params.items())}")
        for prefix, response in self.routes:
            if url.startswith(prefix):
                return response() if callable(response) else response
        raise AssertionError(f"unexpected request {url}")


def twitter_downloader(routes, tmp: str) -> tuple[MediaDownloader, FakeSession]:
    downloader = MediaDownloader.__new__(MediaDownloader)
    downloader.config = SimpleNamespace(
        download_dir=Path(tmp), ytdlp_format="bv*+ba/best", ytdlp_no_playlist=True,
        ytdlp_cookies=None, ytdlp_cookies_from_browser=None,
        api_base="https://api.telegram.org", max_upload_bytes=50 * 1024 * 1024,
    )
    session = FakeSession(routes)
    downloader._local = threading.local()
    downloader._local.session = session
    return downloader, session


class TwitterPhotoDownloadTests(unittest.TestCase):
    URL = "https://x.com/jack/status/1788888888888888888"

    def image_routes(self):
        return [
            ("https://pbs.twimg.com/media/AAA.jpg", FakeResponse(content=b"A" * 10, headers={"Content-Type": "image/jpeg"})),
            ("https://pbs.twimg.com/media/BBB.png", FakeResponse(content=b"B" * 20, headers={"Content-Type": "image/png"})),
            ("https://pbs.twimg.com/media/CCC.jpg", FakeResponse(content=b"C" * 5, headers={"Content-Type": "image/jpeg"})),
        ]

    def test_photos_come_from_the_syndication_endpoint_in_original_size_and_order(self) -> None:
        routes = [("https://cdn.syndication.twimg.com/", FakeResponse(json_data=SYNDICATION_PHOTOS))] + self.image_routes()
        with tempfile.TemporaryDirectory() as tmp:
            downloader, session = twitter_downloader(routes, tmp)

            media = downloader.download_twitter_photos(self.URL, Path(tmp))

            self.assertEqual([p.name for p in media.paths], ["tweet-photo-001.jpg", "tweet-photo-002.png"])
            self.assertEqual([p.read_bytes()[:1] for p in media.paths], [b"A", b"B"])
            self.assertEqual((media.title, media.uploader, media.webpage_url), ("two photos & a caption", "Jack", self.URL))
            self.assertTrue(media.is_album)
            self.assertTrue(session.requests[0].startswith("https://cdn.syndication.twimg.com/tweet-result?id=1788888888888888888&token="))
            self.assertEqual(session.requests[1:], ["https://pbs.twimg.com/media/AAA.jpg?name=orig", "https://pbs.twimg.com/media/BBB.png?name=orig"])

    def test_single_photo_tweet_is_a_single_photo(self) -> None:
        data = {**SYNDICATION_PHOTOS, "mediaDetails": SYNDICATION_PHOTOS["mediaDetails"][:1]}
        routes = [("https://cdn.syndication.twimg.com/", FakeResponse(json_data=data))] + self.image_routes()
        with tempfile.TemporaryDirectory() as tmp:
            downloader, _session = twitter_downloader(routes, tmp)

            media = downloader.download_twitter_photos(self.URL, Path(tmp))

            self.assertEqual(len(media.paths), 1)
            self.assertFalse(media.is_album)
            self.assertEqual(media.kinds, ("photo",))

    def test_falls_back_to_fxtwitter_when_syndication_fails_or_is_a_tombstone(self) -> None:
        for syndication in (FakeResponse(status=500), FakeResponse(json_data={"__typename": "TweetTombstone"})):
            with self.subTest(syndication=syndication.status), tempfile.TemporaryDirectory() as tmp:
                routes = [
                    ("https://cdn.syndication.twimg.com/", syndication),
                    ("https://api.fxtwitter.com/", FakeResponse(json_data=FXTWITTER_PHOTOS)),
                ] + self.image_routes()
                downloader, session = twitter_downloader(routes, tmp)

                media = downloader.download_twitter_photos(self.URL, Path(tmp))

                self.assertEqual([p.read_bytes() for p in media.paths], [b"C" * 5])
                self.assertIn("https://api.fxtwitter.com/jack/status/1788888888888888888", session.requests)

    def test_text_only_tweets_do_not_hit_the_third_party_api(self) -> None:
        routes = [
            ("https://cdn.syndication.twimg.com/", FakeResponse(json_data={"text": "just words", "user": {"name": "Jack"}})),
            ("https://api.fxtwitter.com/", lambda: (_ for _ in ()).throw(AssertionError("must not be called"))),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            downloader, session = twitter_downloader(routes, tmp)

            with self.assertRaisesRegex(RuntimeError, "did not expose any photos"):
                downloader.download_twitter_photos(self.URL, Path(tmp))
            self.assertEqual(len(session.requests), 1)

    def test_unreadable_tweets_report_both_failures(self) -> None:
        routes = [
            ("https://cdn.syndication.twimg.com/", FakeResponse(status=403)),
            ("https://api.fxtwitter.com/", FakeResponse(json_data={"code": 404})),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            downloader, _session = twitter_downloader(routes, tmp)

            with self.assertRaisesRegex(RuntimeError, r"Could not read the tweet \(syndication: .*; fxtwitter: no tweet data\)"):
                downloader.download_twitter_photos(self.URL, Path(tmp))

    def test_photos_too_big_for_telegram_are_refetched_in_large_size(self) -> None:
        routes = [
            ("https://pbs.twimg.com/media/AAA.jpg?name=orig", FakeResponse(content=b"O" * 100, headers={"Content-Type": "image/jpeg"})),
            ("https://pbs.twimg.com/media/AAA.jpg?name=large", FakeResponse(content=b"L" * 10, headers={"Content-Type": "image/jpeg"})),
        ]
        with tempfile.TemporaryDirectory() as tmp, mock.patch("tiktok_downloader_bot.downloader.TELEGRAM_PHOTO_MAX_BYTES", 50):
            downloader, session = twitter_downloader(routes, tmp)

            path = downloader.download_twitter_image("https://pbs.twimg.com/media/AAA.jpg", Path(tmp), index=1)

            self.assertEqual(path.read_bytes(), b"L" * 10)
            self.assertEqual(len(session.requests), 2)

    def test_bad_status_links_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            downloader, _session = twitter_downloader([], tmp)
            with self.assertRaisesRegex(RuntimeError, "not a tweet link"):
                downloader.download_twitter_photos("https://x.com/jack", Path(tmp))


class TwitterFallbackFlowTests(unittest.TestCase):
    """yt-dlp cannot download photo tweets: the downloader must fall back to the photos."""

    URL = "https://x.com/jack/status/1788888888888888888"
    NO_VIDEO = RuntimeError("ERROR: [twitter] 1788888888888888888: No video could be found in this tweet")

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        routes = [
            ("https://cdn.syndication.twimg.com/", FakeResponse(json_data=SYNDICATION_PHOTOS)),
            ("https://pbs.twimg.com/media/AAA.jpg", FakeResponse(content=b"A", headers={"Content-Type": "image/jpeg"})),
            ("https://pbs.twimg.com/media/BBB.png", FakeResponse(content=b"B", headers={"Content-Type": "image/png"})),
        ]
        self.downloader, self.session = twitter_downloader(routes, self.tmp.name)
        self.attempts: list[str] = []

    def failing_ytdlp(self, error: BaseException):
        def run(_self, download_url, attempt_dir, strategy, **_kwargs):
            self.attempts.append(strategy.name)
            raise error

        return mock.patch.object(MediaDownloader, "run_ytdlp", run)

    def test_photo_tweet_is_downloaded_after_one_ytdlp_attempt(self) -> None:
        with self.failing_ytdlp(self.NO_VIDEO), self.downloader.download(self.URL) as media:
            self.assertEqual(len(media.paths), 2)
            self.assertEqual(media.kinds, ("photo", "photo"))
            self.assertTrue(all(path.exists() for path in media.paths))
            job_dir = media.paths[0].parent

        self.assertEqual(self.attempts, ["requested"])
        self.assertFalse(job_dir.exists())  # temp files are cleaned up

    def test_mirror_links_are_rewritten_to_x_before_downloading(self) -> None:
        seen: list[str] = []

        def fake(_self, download_url, job_dir, **_kwargs):
            seen.append(download_url)
            return DownloadedMedia(paths=(), title="t")

        with mock.patch.object(MediaDownloader, "download_with_ytdlp", fake):
            with self.downloader.download("https://fxtwitter.com/jack/status/20?s=1"):
                pass

        self.assertEqual(seen, ["https://x.com/jack/status/20"])

    def test_tweets_without_video_or_photos_fail_fast_with_a_clear_reason(self) -> None:
        self.session.routes[0] = ("https://cdn.syndication.twimg.com/", FakeResponse(json_data={"text": "words"}))

        with self.failing_ytdlp(self.NO_VIDEO), self.assertRaises(RuntimeError) as caught:
            with self.downloader.download(self.URL):
                pass

        self.assertEqual(self.attempts, ["requested"])  # no pointless format retries
        self.assertEqual(friendly_error_message(caught.exception), "Could not find downloadable media metadata.")

    def test_other_ytdlp_failures_still_try_the_remaining_format_strategies(self) -> None:
        self.session.routes[0] = ("https://cdn.syndication.twimg.com/", FakeResponse(status=500))
        self.session.routes.insert(1, ("https://api.fxtwitter.com/", FakeResponse(status=500)))

        with self.failing_ytdlp(RuntimeError("HTTP Error 500")), self.assertRaises(RuntimeError):
            with self.downloader.download(self.URL):
                pass

        self.assertGreater(len(self.attempts), 1)
        self.assertEqual(len([r for r in self.session.requests if "syndication" in r]), 1)  # photo fallback only once

    def test_audio_requests_never_use_the_photo_fallback(self) -> None:
        with self.failing_ytdlp(self.NO_VIDEO), self.assertRaises(RuntimeError):
            with self.downloader.download(self.URL, audio_only=True):
                pass

        self.assertEqual(self.session.requests, [])

    def test_non_twitter_links_do_not_use_the_fallback(self) -> None:
        with self.failing_ytdlp(self.NO_VIDEO), self.assertRaises(RuntimeError):
            with self.downloader.download("https://www.instagram.com/p/abc/"):
                pass

        self.assertEqual(self.session.requests, [])
