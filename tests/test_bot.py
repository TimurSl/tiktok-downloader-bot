import tempfile
import threading
import time
import unittest
from unittest import mock
from collections import Counter, OrderedDict
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any

from tiktok_downloader_bot.bot import (
    DownloadSession,
    DownloaderBot,
    ExtraRequest,
    StopPolling,
    cache_key,
    help_text,
    is_group_chat,
    parse_command,
    settings_keyboard,
    strip_bot_mention,
)
from tiktok_downloader_bot.downloader import DownloadedMedia, QualityOption, quality_format_selector
from tiktok_downloader_bot.errors import DownloadCancelled, NoSoundError
from tiktok_downloader_bot.storage import FileIdCache, SettingsStore
from tiktok_downloader_bot.telegram_api import ChatContext, TelegramAPIError


class FakeDownloader:
    def __init__(self, media: DownloadedMedia | None = None, error: BaseException | None = None) -> None:
        self.media = media
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.options: list[QualityOption] = [QualityOption("auto", "Auto", None)]

    def download(self, url: str, **kwargs: Any) -> "FakeDownloader":
        self.calls.append((url, kwargs))
        return self

    def __enter__(self) -> DownloadedMedia:
        if self.error:
            raise self.error
        return self.media

    def __exit__(self, *_args: object) -> None:
        return None

    def youtube_quality_options(self, _url: str) -> list[QualityOption]:
        return self.options


class FakeTelegramAPI:
    def __init__(self, *, fail_album_calls: int = 0, fail_sends_of: tuple[str, ...] = ()) -> None:
        self.fail_album_calls = fail_album_calls
        self.fail_sends_of = fail_sends_of
        self.callback_answers: list[tuple[str, str]] = []
        self.edits: list[tuple[int, int, str, Any]] = []
        self.markup_edits: list[tuple[int, int]] = []
        self.markup_values: list[Any] = []
        self.deletions: list[tuple[int, int]] = []
        self.messages: list[tuple[Any, str, ChatContext, Any]] = []
        self.media: list[dict[str, Any]] = []
        self.albums: list[dict[str, Any]] = []
        self.inline_answers: list[tuple[str, list[dict[str, Any]]]] = []
        self.inline_media_edits: list[tuple[str, dict[str, Any]]] = []
        self.inline_text_edits: list[tuple[str, str]] = []
        self.chat_members: dict[tuple[Any, int], str] = {}
        self.next_message_id = 100
        self.next_file_id = 0

    def get_chat_member(self, chat_id: Any, user_id: int) -> dict[str, Any]:
        return {"status": self.chat_members.get((chat_id, user_id), "member")}

    def answer_callback_query(self, callback_query_id: str, text: str = "", *, alert: bool = False) -> bool:
        self.callback_answers.append((callback_query_id, text))
        return True

    def send_message(self, context: ChatContext, text: str, *, reply_markup: Any = None, parse_mode: Any = None) -> dict[str, Any]:
        self.messages.append((context.chat_id, text, context, reply_markup))
        self.next_message_id += 1
        return {"message_id": self.next_message_id}

    def edit_message_text(self, context: ChatContext, message_id: int, text: str, *, reply_markup: Any = None) -> bool:
        self.edits.append((context.chat_id, message_id, text, reply_markup))
        return True

    def edit_message_reply_markup(self, context: ChatContext, message_id: int, reply_markup: Any = None) -> bool:
        self.markup_edits.append((context.chat_id, message_id))
        self.markup_values.append(reply_markup)
        return True

    def delete_message(self, context: ChatContext, message_id: int) -> bool:
        self.deletions.append((context.chat_id, message_id))
        return True

    def _file_id(self) -> str:
        self.next_file_id += 1
        return f"file-{self.next_file_id}"

    def send_media(self, context: ChatContext, source: Any, *, kind: str | None = None, **kwargs: Any) -> dict[str, Any]:
        kind = kind or "document"
        if kind in self.fail_sends_of and isinstance(source, str):
            raise TelegramAPIError("sendVideo failed: wrong file identifier")
        self.media.append({"context": context, "source": source, "kind": kind, **kwargs})
        file_id = self._file_id()
        self.next_message_id += 1
        if kind == "photo":
            return {"message_id": self.next_message_id, "photo": [{"file_id": file_id}]}
        return {"message_id": self.next_message_id, kind: {"file_id": file_id}}

    def send_media_group(self, context: ChatContext, items: list[tuple[str, Any]], **kwargs: Any) -> list[dict[str, Any]]:
        self.albums.append({"context": context, "items": items, **kwargs})
        if self.fail_album_calls:
            self.fail_album_calls -= 1
            raise TelegramAPIError("sendMediaGroup failed: upload failed")
        messages = []
        for kind, _source in items:
            file_id = self._file_id()
            messages.append({"photo": [{"file_id": file_id}]} if kind == "photo" else {kind: {"file_id": file_id}})
        return messages

    def answer_inline_query(self, inline_query_id: str, results: list[dict[str, Any]], **_kwargs: Any) -> bool:
        self.inline_answers.append((inline_query_id, results))
        return True

    def edit_inline_message_media(self, inline_message_id: str, media: dict[str, Any]) -> bool:
        self.inline_media_edits.append((inline_message_id, media))
        return True

    def edit_inline_message_text(self, inline_message_id: str, text: str) -> bool:
        self.inline_text_edits.append((inline_message_id, text))
        return True


class ImmediateScheduler:
    """Runs jobs inline so tests stay deterministic."""

    def __init__(self) -> None:
        self.submitted: list[Any] = []
        self.accept = True

    def submit(self, key: Any, job: Any) -> bool:
        self.submitted.append(key)
        if not self.accept:
            return False
        job()
        return True

    def counts(self) -> tuple[int, int]:
        return 0, 0


def make_media_file(suffix: str, size: int = 4) -> Path:
    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        handle.write(b"d" * size)
        return Path(handle.name)
    finally:
        handle.close()


def make_bot(
    *,
    authorized: frozenset[int] = frozenset(),
    admins: frozenset[int] = frozenset(),
    api: FakeTelegramAPI | None = None,
    downloader: FakeDownloader | None = None,
    cache_chat_id: str | None = None,
) -> DownloaderBot:
    bot = DownloaderBot.__new__(DownloaderBot)
    bot.config = SimpleNamespace(
        authorized_users=authorized,
        admin_users=admins,
        api_base="https://api.telegram.org",
        max_upload_bytes=50 * 1024 * 1024,
        max_links_per_message=3,
        cache_chat_id=cache_chat_id,
    )
    bot.api = api or FakeTelegramAPI()
    bot.downloader = downloader or FakeDownloader()
    bot.settings = SettingsStore(None)
    bot.file_cache = FileIdCache(None)
    bot.scheduler = ImmediateScheduler()
    bot.sessions = {}
    bot.extra_requests = OrderedDict()
    bot.state_lock = threading.Lock()
    bot.started_at = time.time()
    bot.stats = Counter()
    bot.last_admin_notice = float("-inf")
    bot.bot_username = "media_bot"
    bot.running = True
    bot.polling = False
    return bot


def message(text: str, *, chat_id: int = 200, chat_type: str = "private", user_id: int = 200, **extra: Any) -> dict[str, Any]:
    return {
        "text": text,
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": user_id},
        "message_id": 10,
        **extra,
    }


def button_labels(markup: dict[str, Any] | None) -> list[str]:
    return [button["text"] for row in (markup or {}).get("inline_keyboard", []) for button in row]


def capture_queue(bot: DownloaderBot) -> list[tuple[list[str], bool]]:
    queued: list[tuple[list[str], bool]] = []
    bot.queue_downloads = MethodType(
        lambda _self, _context, urls, _settings, *, audio: queued.append((list(urls), audio)), bot
    )
    return queued


def make_session(url: str = "https://www.tiktok.com/@a/video/1", *, settings: dict[str, Any] | None = None, **context_kwargs: Any) -> DownloadSession:
    context = ChatContext(chat_id=-100, requester_id=200, source_message_id=10, **context_kwargs)
    return DownloadSession(
        token="token",
        url=url,
        context=context,
        requester_id=200,
        created_at=time.time(),
        settings=settings if settings is not None else SettingsStore(None).get(-100),
    )


class CommandParsingTests(unittest.TestCase):
    def test_parses_commands_with_targets(self) -> None:
        command = parse_command("/Audio@media_bot  https://x.test/a", "media_bot")
        self.assertEqual((command.name, command.args, command.addressed, command.foreign), ("audio", "https://x.test/a", True, False))

        foreign = parse_command("/start@other_bot", "media_bot")
        self.assertTrue(foreign.foreign)
        self.assertFalse(foreign.addressed)

        self.assertIsNone(parse_command("hello /start", "media_bot"))
        self.assertEqual(parse_command("/start", "media_bot").args, "")

    def test_strip_mention_and_group_detection(self) -> None:
        self.assertEqual(strip_bot_mention("@Media_Bot https://x.test", "media_bot"), "https://x.test")
        self.assertTrue(is_group_chat({"chat": {"type": "group"}}))
        self.assertTrue(is_group_chat({"chat": {"type": "supergroup"}}))
        self.assertFalse(is_group_chat({"chat": {"type": "private"}}))


class MessageHandlingTests(unittest.TestCase):
    def test_authorized_chat_ignores_non_media_links(self) -> None:
        bot = make_bot()
        queued = capture_queue(bot)

        bot.handle_message(message("please read https://example.test/docs instead"))

        self.assertEqual(queued, [])
        self.assertEqual(bot.api.messages, [])

    def test_message_downloads_later_media_link_instead_of_first_ordinary_link(self) -> None:
        bot = make_bot()
        queued = capture_queue(bot)

        bot.handle_message(message("context https://example.test/docs video https://www.instagram.com/reel/abc123/"))

        self.assertEqual(queued, [(["https://www.instagram.com/reel/abc123/"], False)])

    def test_every_supported_link_in_a_message_is_queued_up_to_the_limit(self) -> None:
        bot = make_bot()
        queued = capture_queue(bot)
        links = [f"https://www.instagram.com/reel/{name}/" for name in "abcd"]

        bot.handle_message(message(" ".join(links)))

        self.assertEqual(queued, [(links[:3], False)])

    def test_group_ordinary_links_do_not_bypass_user_allowlist(self) -> None:
        bot = make_bot(authorized=frozenset({100}))
        queued = capture_queue(bot)

        bot.handle_message(message("https://example.test/docs", chat_id=-100, chat_type="supergroup"))

        self.assertEqual(queued, [])
        self.assertEqual(bot.api.messages, [])

    def test_group_media_links_bypass_user_allowlist(self) -> None:
        bot = make_bot(authorized=frozenset({100}))
        queued = capture_queue(bot)

        bot.handle_message(message("@media_bot https://vm.tiktok.com/ZMh123/", chat_id=-100, chat_type="supergroup"))

        self.assertEqual(queued, [(["https://vm.tiktok.com/ZMh123/"], False)])

    def test_unauthorized_group_commands_are_ignored_unless_addressed_to_this_bot(self) -> None:
        bot = make_bot(authorized=frozenset({100}))
        capture_queue(bot)

        bot.handle_message(message("/settings", chat_id=-100, chat_type="group"))
        self.assertEqual(bot.api.messages, [])
        bot.handle_message(message("/settings@media_bot", chat_id=-100, chat_type="group"))
        self.assertEqual(bot.api.messages[-1][1], "You are not authorized to use this bot.")

    def test_private_media_links_still_require_allowlist_and_the_user_is_told(self) -> None:
        bot = make_bot(authorized=frozenset({100}))
        queued = capture_queue(bot)

        bot.handle_message(message("https://vm.tiktok.com/ZMh123/"))

        self.assertEqual(queued, [])
        self.assertEqual(bot.api.messages[-1][1], "You are not authorized to use this bot.")

    def test_private_plain_text_from_unauthorized_user_is_ignored(self) -> None:
        bot = make_bot(authorized=frozenset({100}))

        bot.handle_message(message("hello there"))

        self.assertEqual(bot.api.messages, [])

    def test_rp_command_is_unknown_not_roleplay(self) -> None:
        bot = make_bot()
        queued = capture_queue(bot)

        bot.handle_message(
            message("/rp add share {actor} shares https://example.test", chat_id=-100, chat_type="supergroup", user_id=100)
        )

        self.assertEqual(queued, [])
        self.assertEqual(bot.api.messages[-1][1], "Unknown command. Send a supported media-platform URL to download it.")

    def test_commands_for_other_bots_are_ignored_and_prefix_lookalikes_are_unknown(self) -> None:
        bot = make_bot()
        capture_queue(bot)

        bot.handle_message(message("/start@other_bot"))
        self.assertEqual(bot.api.messages, [])
        bot.handle_message(message("/startxyz"))
        self.assertEqual(bot.api.messages[-1][1], "Unknown command. Send a supported media-platform URL to download it.")
        bot.handle_message(message("/help@media_bot"))
        self.assertTrue(bot.api.messages[-1][1].startswith("Send me a link"))

    def test_replies_use_the_senders_language(self) -> None:
        bot = make_bot()

        bot.handle_message({**message("/start"), "from": {"id": 200, "language_code": "ru"}})

        self.assertTrue(bot.api.messages[-1][1].startswith("Отправьте ссылку"))

    def test_replies_are_silent_and_not_threaded_by_default(self) -> None:
        bot = make_bot()

        bot.handle_message(message("/start"))

        context = bot.api.messages[-1][2]
        self.assertIsNone(context.reply_to_message_id)
        self.assertTrue(context.silent)

    def test_reply_setting_turns_threaded_replies_on(self) -> None:
        bot = make_bot()
        bot.settings.toggle(200, "reply")

        bot.handle_message(message("/start"))

        self.assertEqual(bot.api.messages[-1][2].reply_to_message_id, 10)

    def test_audio_command_uses_link_from_args_or_replied_message(self) -> None:
        bot = make_bot()
        queued = capture_queue(bot)

        bot.handle_message(message("/audio https://www.youtube.com/watch?v=abc"))
        bot.handle_message(
            message("/audio", reply_to_message={"text": "look https://vm.tiktok.com/ZM1/"})
        )
        bot.handle_message(message("/audio"))

        self.assertEqual(
            queued,
            [(["https://www.youtube.com/watch?v=abc"], True), (["https://vm.tiktok.com/ZM1/"], True)],
        )
        self.assertTrue(bot.api.messages[-1][1].startswith("Usage: /audio"))

    def test_group_auto_download_off_requires_a_mention_or_command(self) -> None:
        bot = make_bot()
        queued = capture_queue(bot)
        bot.settings.toggle(-100, "auto")

        bot.handle_message(message("https://vm.tiktok.com/ZM1/", chat_id=-100, chat_type="group"))
        self.assertEqual(queued, [])
        bot.handle_message(message("@media_bot https://vm.tiktok.com/ZM1/", chat_id=-100, chat_type="group"))
        bot.handle_message(message("/dl https://vm.tiktok.com/ZM2/", chat_id=-100, chat_type="group"))
        bot.handle_message(message("https://vm.tiktok.com/ZM3/", chat_type="private"))

        self.assertEqual([urls for urls, _ in queued], [["https://vm.tiktok.com/ZM1/"], ["https://vm.tiktok.com/ZM2/"], ["https://vm.tiktok.com/ZM3/"]])

    def test_busy_users_get_a_notice_and_remaining_links_are_dropped(self) -> None:
        bot = make_bot()
        bot.scheduler.accept = False

        bot.handle_message(message("https://vm.tiktok.com/ZM1/ https://vm.tiktok.com/ZM2/"))

        self.assertEqual(len(bot.scheduler.submitted), 1)
        self.assertIn("too many downloads", bot.api.messages[-1][1])

    def test_unexpected_handler_errors_are_contained_and_reported_to_admins(self) -> None:
        bot = make_bot(admins=frozenset({1}))
        bot.handle_message = MethodType(lambda _self, _message: 1 / 0, bot)

        bot.safe_handle_update({"update_id": 5, "message": {}})

        self.assertEqual(bot.api.messages[-1][0], 1)
        self.assertFalse(bot.api.messages[-1][2].silent)
        self.assertIn("Unhandled error", bot.api.messages[-1][1])

    def test_admin_notices_are_rate_limited_and_optional(self) -> None:
        bot = make_bot()
        bot.notify_admins("nobody to tell")
        self.assertEqual(bot.api.messages, [])

        bot = make_bot(admins=frozenset({1, 2}))
        bot.notify_admins("first")
        bot.notify_admins("second")
        self.assertEqual([m[0] for m in bot.api.messages], [1, 2])

    def test_stats_command_is_admin_only(self) -> None:
        bot = make_bot(admins=frozenset({200}))
        bot.stats["downloads_ok"] = 3
        bot.handle_message(message("/stats"))
        self.assertIn("Downloads: 3 ok", bot.api.messages[-1][1])

        other = make_bot(admins=frozenset({1}))
        other.handle_message(message("/stats"))
        self.assertTrue(other.api.messages[-1][1].startswith("Unknown command"))

    def test_guest_mode_updates_are_no_longer_handled(self) -> None:
        bot = make_bot()
        bot.handle_update({"update_id": 1, "guest_message": {"guest_query_id": "q", "text": "https://vm.tiktok.com/ZM1/"}})
        self.assertEqual(bot.api.messages, [])
        self.assertFalse(hasattr(bot, "handle_guest_message"))
        self.assertNotIn("guest", help_text("en", "media_bot", True).lower())


class PrepareDownloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bot = make_bot()
        self.runs: list[DownloadSession] = []
        self.bot.run_download_session = MethodType(
            lambda _self, session, *, callback_message_id=None: self.runs.append(session), self.bot
        )

    def context(self) -> ChatContext:
        return ChatContext(chat_id=200, requester_id=200, source_message_id=10)

    def test_plain_links_download_immediately(self) -> None:
        self.bot.prepare_download(self.context(), "https://vm.tiktok.com/ZM1/", self.bot.settings.get(200))

        self.assertEqual(len(self.runs), 1)
        self.assertIsNone(self.runs[0].selected_quality)

    def test_youtube_links_ask_for_quality_by_default(self) -> None:
        self.bot.downloader.options = [QualityOption("auto", "Auto", None), QualityOption("audio", "Audio", "bestaudio/best", audio_only=True)]

        self.bot.prepare_download(self.context(), "https://youtu.be/abc", self.bot.settings.get(200))

        self.assertEqual(self.runs, [])
        text, markup = self.bot.api.messages[-1][1], self.bot.api.messages[-1][3]
        self.assertEqual(text, "Choose quality:")
        self.assertEqual(markup["inline_keyboard"][-1][0]["callback_data"].endswith(":cancel"), True)
        session = next(iter(self.bot.sessions.values()))
        self.assertEqual(session.status_message_id, 101)
        self.assertEqual(set(session.options), {"auto", "audio"})

    def test_default_quality_setting_skips_the_prompt(self) -> None:
        settings = self.bot.settings.get(200) | {"quality": "720"}
        self.bot.prepare_download(self.context(), "https://youtu.be/abc", settings)
        self.assertEqual(self.runs[0].selected_quality.format_selector, quality_format_selector(720))
        self.assertEqual(self.runs[0].selected_quality.max_height, 720)

        self.runs.clear()
        self.bot.downloader.options = [
            QualityOption("auto", "Auto", quality_format_selector(480), max_height=480),
            QualityOption("h480", "480p", quality_format_selector(480), max_height=480),
        ]
        self.bot.prepare_download(self.context(), "https://youtu.be/abc", settings | {"quality": "auto"})
        self.assertEqual(self.runs[0].selected_quality.max_height, 480)
        self.assertEqual(self.bot.api.messages, [])

    def test_audio_flag_and_audio_default_download_sound_only(self) -> None:
        self.bot.prepare_download(self.context(), "https://youtu.be/abc", self.bot.settings.get(200), audio=True)
        self.bot.prepare_download(self.context(), "https://vm.tiktok.com/ZM1/", self.bot.settings.get(200) | {"quality": "audio"})

        self.assertTrue(all(session.is_audio for session in self.runs))
        self.assertEqual(self.bot.api.messages, [])

    def test_option_lookup_failure_is_reported_with_retry_button(self) -> None:
        def boom(_url: str) -> list[QualityOption]:
            raise RuntimeError("ERROR: Sign in to confirm you're not a bot")

        self.bot.downloader.youtube_quality_options = boom

        self.bot.prepare_download(self.context(), "https://youtu.be/abc", self.bot.settings.get(200))

        self.assertEqual(self.runs, [])
        self.assertIn("cookies", self.bot.api.messages[-1][1])
        self.assertEqual(self.bot.api.messages[-1][3]["inline_keyboard"][0][0]["text"], "Retry")


class DownloadFlowTests(unittest.TestCase):
    def video(self, **kwargs: Any) -> DownloadedMedia:
        defaults = dict(
            paths=(make_media_file(".mp4"),), title="funny cat #fyp", uploader="Bob",
            webpage_url="https://www.tiktok.com/@bob/video/1", duration=12, width=720, height=1280,
        )
        defaults.update(kwargs)
        return DownloadedMedia(**defaults)

    def test_video_is_sent_with_metadata_caption_and_an_unsent_sound_button(self) -> None:
        bot = make_bot(downloader=FakeDownloader(self.video()))
        session = make_session()
        bot.sessions["token"] = session

        bot.run_download_session(session)

        sent = bot.api.media[0]
        self.assertEqual(sent["kind"], "video")
        self.assertEqual(sent["parse_mode"], "HTML")
        self.assertIn("funny cat", sent["caption"])
        self.assertNotIn("#fyp", sent["caption"])
        self.assertIn('<a href="https://www.tiktok.com/@bob/video/1">tiktok.com</a>', sent["caption"])
        self.assertEqual((sent["meta"].width, sent["meta"].height, sent["meta"].duration), (720, 1280, 12))
        button = sent["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(button["text"], "🎵 Sound")
        self.assertTrue(button["callback_data"].startswith("snd:"))
        self.assertEqual([m for m in bot.api.media if m["kind"] == "audio"], [])  # sound is never sent unasked
        # status message was shown, then removed; session finished
        self.assertEqual(bot.api.messages[0][1], "Downloading…")
        self.assertEqual(bot.api.deletions, [(-100, 101)])
        self.assertNotIn("token", bot.sessions)
        self.assertEqual(bot.stats["downloads_ok"], 1)

    def test_nothing_is_sent_as_a_reply_by_default(self) -> None:
        bot = make_bot(downloader=FakeDownloader(self.video()))
        session = make_session()
        bot.run_download_session(session)

        self.assertIsNone(bot.api.media[0]["context"].reply_to_message_id)
        self.assertIsNone(bot.api.messages[0][2].reply_to_message_id)

    def test_sound_button_can_be_disabled_per_chat(self) -> None:
        bot = make_bot(downloader=FakeDownloader(self.video()))
        session = make_session(settings=SettingsStore(None).get(1) | {"sound": False, "gif": False})

        bot.run_download_session(session)

        self.assertIsNone(bot.api.media[0]["reply_markup"])
        self.assertEqual(bot.extra_requests, OrderedDict())

    def test_each_extra_button_can_be_disabled_on_its_own(self) -> None:
        bot = make_bot(downloader=FakeDownloader(self.video()))
        bot.run_download_session(make_session(settings=SettingsStore(None).get(1) | {"sound": False}))
        self.assertEqual(button_labels(bot.api.media[0]["reply_markup"]), ["🎞 GIF"])

        bot = make_bot(downloader=FakeDownloader(self.video()))
        bot.run_download_session(make_session(settings=SettingsStore(None).get(1) | {"gif": False}))
        self.assertEqual(button_labels(bot.api.media[0]["reply_markup"]), ["🎵 Sound"])

    def test_videos_get_sound_and_gif_buttons_in_one_row(self) -> None:
        bot = make_bot(downloader=FakeDownloader(self.video()))

        bot.run_download_session(make_session())

        markup = bot.api.media[0]["reply_markup"]
        self.assertEqual(len(markup["inline_keyboard"]), 1)
        self.assertEqual(button_labels(markup), ["🎵 Sound", "🎞 GIF"])
        callbacks = [button["callback_data"] for button in markup["inline_keyboard"][0]]
        self.assertTrue(callbacks[0].startswith("snd:") and callbacks[1].startswith("gif:"))
        self.assertEqual(callbacks[0][4:], callbacks[1][4:])
        self.assertEqual(bot.api.media[0]["kind"], "video")
        self.assertEqual([m for m in bot.api.media if m["kind"] in {"audio", "animation"}], [])  # nothing unasked

    def test_long_videos_get_no_gif_button_but_keep_the_sound_button(self) -> None:
        bot = make_bot(downloader=FakeDownloader(self.video(duration=61)))

        bot.run_download_session(make_session())

        self.assertEqual(button_labels(bot.api.media[0]["reply_markup"]), ["🎵 Sound"])

    def test_photos_albums_audio_and_cached_long_videos_never_offer_a_gif(self) -> None:
        bot = make_bot(downloader=FakeDownloader(DownloadedMedia(paths=(make_media_file(".jpg"),), title="p")))
        bot.run_download_session(make_session("https://www.tiktok.com/@bob/photo/1"))
        self.assertEqual(button_labels(bot.api.media[0]["reply_markup"]), ["🎵 Sound"])

        audio = make_bot(downloader=FakeDownloader(DownloadedMedia(paths=(make_media_file(".mp3"),), title="s")))
        audio.run_download_session(make_session("https://soundcloud.com/a/b"))
        self.assertIsNone(audio.api.media[0]["reply_markup"])

        cached = make_bot(downloader=FakeDownloader(self.video(duration=300)))
        url = "https://www.tiktok.com/@bob/video/1"
        cached.run_download_session(make_session(url))
        cached.run_download_session(make_session(url))  # second send comes from the cache
        self.assertEqual(button_labels(cached.api.media[1]["reply_markup"]), ["🎵 Sound"])

    def test_captions_can_be_disabled_per_chat(self) -> None:
        bot = make_bot(downloader=FakeDownloader(self.video()))
        session = make_session(settings=SettingsStore(None).get(1) | {"captions": False})

        bot.run_download_session(session)

        self.assertEqual(bot.api.media[0]["caption"], "")

    def test_tiktok_photo_album_gets_a_silent_helper_message_with_the_sound_button(self) -> None:
        paths = tuple(make_media_file(".jpg") for _ in range(3))
        bot = make_bot(downloader=FakeDownloader(DownloadedMedia(paths=paths, title="photos", uploader="Bob", webpage_url="https://www.tiktok.com/@bob/photo/1")))
        session = make_session("https://www.tiktok.com/@bob/photo/1")

        bot.run_download_session(session)

        self.assertEqual([kind for kind, _ in bot.api.albums[0]["items"]], ["photo"] * 3)
        helper = [m for m in bot.api.messages if m[1] == "🎵 Want the sound of this post?"][0]
        callback = helper[3]["inline_keyboard"][0][0]["callback_data"]
        request = bot.extra_requests[callback[4:]]
        self.assertTrue(request.is_helper)
        self.assertEqual(request.url, "https://www.tiktok.com/@bob/photo/1")
        self.assertEqual([m for m in bot.api.media if m["kind"] == "audio"], [])

    def test_single_tiktok_photo_carries_the_button_itself(self) -> None:
        bot = make_bot(downloader=FakeDownloader(DownloadedMedia(paths=(make_media_file(".jpg"),), title="p")))
        bot.run_download_session(make_session("https://www.tiktok.com/@bob/photo/1"))

        self.assertTrue(bot.api.media[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith("snd:"))

    def test_non_tiktok_photos_and_audio_files_get_no_sound_button(self) -> None:
        paths = tuple(make_media_file(".jpg") for _ in range(2))
        bot = make_bot(downloader=FakeDownloader(DownloadedMedia(paths=paths, title="insta")))
        bot.run_download_session(make_session("https://www.instagram.com/p/abc/"))
        self.assertEqual([m for m in bot.api.messages if "sound" in m[1].lower()], [])

        bot = make_bot(downloader=FakeDownloader(DownloadedMedia(paths=(make_media_file(".mp3"),), title="song", performer="Artist")))
        bot.run_download_session(make_session("https://soundcloud.com/a/b"))
        self.assertEqual(bot.api.media[0]["kind"], "audio")
        self.assertIsNone(bot.api.media[0]["reply_markup"])
        self.assertEqual(bot.api.media[0]["meta"].performer, "Artist")
        self.assertEqual(bot.api.media[0]["meta"].title, "song")

    def test_audio_only_download_has_no_sound_button_and_requests_audio(self) -> None:
        downloader = FakeDownloader(DownloadedMedia(paths=(make_media_file(".mp3"),), title="song", uploader="U", is_audio_only=True))
        bot = make_bot(downloader=downloader)
        session = make_session("https://www.tiktok.com/@bob/video/1")
        session.audio_only = True

        bot.run_download_session(session)

        self.assertTrue(downloader.calls[0][1]["audio_only"])
        self.assertEqual(bot.api.media[0]["kind"], "audio")
        self.assertIsNone(bot.api.media[0]["reply_markup"])
        self.assertNotIn("song", bot.api.media[0]["caption"])  # the audio tag already shows the title

    def test_mixed_photo_video_post_is_sent_as_one_album(self) -> None:
        paths = (make_media_file(".jpg"), make_media_file(".mp4"), make_media_file(".jpg"))
        bot = make_bot(downloader=FakeDownloader(DownloadedMedia(paths=paths, title="carousel")))

        bot.run_download_session(make_session("https://www.instagram.com/p/abc/"))

        self.assertEqual([kind for kind, _ in bot.api.albums[0]["items"]], ["photo", "video", "photo"])

    def test_chosen_quality_and_height_cap_reach_the_downloader(self) -> None:
        downloader = FakeDownloader(self.video())
        bot = make_bot(downloader=downloader)
        session = make_session("https://youtu.be/abc")
        session.selected_quality = QualityOption("h720", "720p", quality_format_selector(720), max_height=720)

        bot.run_download_session(session)

        kwargs = downloader.calls[0][1]
        self.assertEqual((kwargs["format_selector"], kwargs["max_height"]), (quality_format_selector(720), 720))
        # Finished downloads clean up for every site, YouTube included.
        self.assertNotIn("token", bot.sessions)
        self.assertEqual(bot.api.deletions, [(-100, 101)])

    def test_repeat_links_are_served_from_the_file_id_cache(self) -> None:
        downloader = FakeDownloader(self.video())
        bot = make_bot(downloader=downloader)

        bot.run_download_session(make_session("https://www.tiktok.com/@bob/video/1"))
        bot.run_download_session(make_session("https://www.tiktok.com/@bob/video/1?_r=1&_t=zz"))

        self.assertEqual(len(downloader.calls), 1)
        self.assertEqual(bot.stats["cache_hits"], 1)
        self.assertIsInstance(bot.api.media[0]["source"], Path)
        self.assertEqual(bot.api.media[1]["source"], "file-1")
        self.assertEqual(bot.api.media[1]["kind"], "video")
        self.assertIn("Bob", bot.api.media[1]["caption"])
        self.assertTrue(bot.api.media[1]["reply_markup"])  # sound button on cached sends too

    def test_cached_albums_are_resent_by_file_id(self) -> None:
        paths = tuple(make_media_file(".jpg") for _ in range(12))
        downloader = FakeDownloader(DownloadedMedia(paths=paths, title="photos"))
        bot = make_bot(downloader=downloader)
        url = "https://www.tiktok.com/@bob/photo/1"

        bot.run_download_session(make_session(url))
        bot.api.albums.clear()
        bot.run_download_session(make_session(url))

        self.assertEqual(len(downloader.calls), 1)
        self.assertEqual([len(album["items"]) for album in bot.api.albums], [10, 2])
        self.assertTrue(all(isinstance(source, str) for album in bot.api.albums for _, source in album["items"]))

    def test_rejected_cached_file_ids_fall_back_to_a_fresh_download(self) -> None:
        downloader = FakeDownloader(self.video())
        api = FakeTelegramAPI(fail_sends_of=("video",))
        bot = make_bot(downloader=downloader, api=api)

        bot.run_download_session(make_session())
        bot.run_download_session(make_session())

        self.assertEqual(len(downloader.calls), 2)
        self.assertEqual(bot.stats["cache_hits"], 0)
        self.assertEqual(bot.stats["downloads_ok"], 2)

    def test_delete_original_removes_the_link_message_after_success_only(self) -> None:
        bot = make_bot(downloader=FakeDownloader(self.video()))
        bot.run_download_session(make_session(settings=SettingsStore(None).get(1) | {"delete_original": True}))
        self.assertIn((-100, 10), bot.api.deletions)

        failing = make_bot(downloader=FakeDownloader(error=RuntimeError("Private video")))
        failing.run_download_session(make_session(settings=SettingsStore(None).get(1) | {"delete_original": True}))
        self.assertNotIn((-100, 10), failing.api.deletions)

    def test_expected_failures_show_a_friendly_message_with_buttons_and_do_not_page_admins(self) -> None:
        bot = make_bot(downloader=FakeDownloader(error=RuntimeError("ERROR: [youtube] Private video")), admins=frozenset({1}))
        session = make_session("https://youtu.be/abc")
        bot.sessions["token"] = session

        bot.run_download_session(session)

        _chat, message_id, text, markup = bot.api.edits[-1]
        self.assertEqual(text, "Media is unavailable or private.")
        self.assertEqual([row[0]["text"] for row in markup["inline_keyboard"]], ["Retry", "Quality", "Cancel"])
        self.assertIn("token", bot.sessions)  # kept so Retry works
        self.assertEqual(bot.stats["downloads_failed"], 1)
        self.assertFalse(any(m[0] == 1 for m in bot.api.messages))
        self.assertFalse(session.running)

    def test_unexpected_failures_are_forwarded_to_admins(self) -> None:
        bot = make_bot(downloader=FakeDownloader(error=ValueError("weird")), admins=frozenset({1}))

        bot.run_download_session(make_session())

        self.assertEqual(bot.api.edits[-1][2], "Download failed.")
        self.assertTrue(any(m[0] == 1 and "weird" in m[1] for m in bot.api.messages))

    def test_oversized_files_are_rejected_before_upload(self) -> None:
        bot = make_bot(downloader=FakeDownloader(self.video(paths=(make_media_file(".mp4"),))))
        bot.config.max_upload_bytes = 2

        bot.run_download_session(make_session())

        self.assertEqual(bot.api.media, [])
        self.assertEqual(bot.api.edits[-1][2], "The file is too large for Telegram upload.")

    def test_cancelled_sessions_do_not_start_or_report_errors(self) -> None:
        bot = make_bot(downloader=FakeDownloader(error=DownloadCancelled("canceled")))
        session = make_session()
        session.cancel_event.set()
        bot.run_download_session(session)
        self.assertEqual(bot.downloader.calls, [])

        session = make_session()
        bot.run_download_session(session)  # downloader raises DownloadCancelled mid-way
        self.assertEqual(bot.api.edits, [])
        self.assertEqual(bot.stats["downloads_failed"], 0)

    def test_retry_edits_the_existing_status_message(self) -> None:
        bot = make_bot(downloader=FakeDownloader(self.video()))
        session = make_session()
        bot.run_download_session(session, callback_message_id=77)

        self.assertEqual(bot.api.edits[0][1:3], (77, "Downloading…"))
        self.assertEqual(bot.api.messages, [])
        self.assertEqual(bot.api.deletions[0], (-100, 77))


class AlbumFallbackTests(unittest.TestCase):
    def test_album_chunk_upload_retries_once_then_sends_items_individually(self) -> None:
        bot = make_bot(api=FakeTelegramAPI(fail_album_calls=2))
        paths = tuple(make_media_file(".jpg") for _ in range(3))
        items = [("photo", path) for path in paths]
        session = make_session()

        uploaded = bot.send_items(session, items, caption="album title", parse_mode=None)

        self.assertEqual(len(bot.api.albums), 2)
        self.assertEqual([m["source"] for m in bot.api.media], list(paths))
        self.assertEqual(bot.api.media[0]["caption"], "album title")
        self.assertEqual(bot.api.media[1]["caption"], "")
        self.assertEqual(len(uploaded), 3)
        self.assertEqual(bot.api.messages[-1][1], "Album upload failed; sent the items one by one.")

    def test_long_albums_are_split_and_only_the_first_album_has_a_caption(self) -> None:
        bot = make_bot()
        items = [("photo", make_media_file(".jpg")) for _ in range(23)]

        uploaded = bot.send_items(make_session(), items, caption="cap", parse_mode="HTML")

        self.assertEqual([len(a["items"]) for a in bot.api.albums], [10, 10, 3])
        self.assertEqual([a["caption"] for a in bot.api.albums], ["cap", "", ""])
        self.assertEqual(len(uploaded), 23)


class CallbackTests(unittest.TestCase):
    def callback(self, data: str, *, user_id: int = 200, message_id: int = 99, chat_id: int = -100, chat_type: str = "supergroup", lang: str | None = None) -> dict[str, Any]:
        sender: dict[str, Any] = {"id": user_id}
        if lang:
            sender["language_code"] = lang
        return {
            "id": "callback-id",
            "from": sender,
            "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat_id, "type": chat_type}},
        }

    def bot_with_session(self, **session_kwargs: Any) -> tuple[DownloaderBot, DownloadSession, list[tuple]]:
        bot = make_bot()
        session = make_session("https://www.youtube.com/watch?v=abc")
        session.options = {"h720": QualityOption("h720", "720p ~80 MB", quality_format_selector(720))}
        for key, value in session_kwargs.items():
            setattr(session, key, value)
        bot.sessions["token"] = session
        calls: list[tuple] = []
        bot.run_download_session = MethodType(
            lambda _self, active, *, callback_message_id=None: calls.append(
                (active.selected_quality.format_selector if active.selected_quality else None, callback_message_id, active.attempt_count)
            ),
            bot,
        )
        return bot, session, calls

    def test_quality_callback_downloads_selected_quality_and_keeps_session(self) -> None:
        bot, _session, calls = self.bot_with_session()

        bot.handle_callback_query(self.callback("dlq:token:h720"))

        self.assertEqual(calls, [(quality_format_selector(720), 99, 0)])
        self.assertIn("token", bot.sessions)
        self.assertEqual(bot.api.callback_answers, [("callback-id", "Downloading 720p ~80 MB…")])

    def test_unknown_quality_and_running_sessions_are_not_started(self) -> None:
        bot, session, calls = self.bot_with_session()

        bot.handle_callback_query(self.callback("dlq:token:h999"))
        session.running = True
        bot.handle_callback_query(self.callback("dlq:token:h720"))
        bot.handle_callback_query(self.callback("dls:token:retry"))

        self.assertEqual(calls, [])
        self.assertEqual(
            [text for _id, text in bot.api.callback_answers], ["Unknown quality choice.", "Downloading…", "Downloading…"]
        )

    def test_retry_callback_reruns_same_url_and_format_and_increments_attempts(self) -> None:
        bot, session, calls = self.bot_with_session(
            selected_quality=QualityOption("h720", "720p", quality_format_selector(720))
        )

        bot.handle_callback_query(self.callback("dls:token:retry"))

        self.assertEqual(calls, [(quality_format_selector(720), 99, 1)])
        self.assertEqual(bot.api.callback_answers, [("callback-id", "Retrying…")])

    def test_quality_reselect_turns_the_status_message_into_the_picker(self) -> None:
        bot, session, _calls = self.bot_with_session()
        bot.downloader.options = [QualityOption("auto", "Auto", None), QualityOption("h360", "360p", quality_format_selector(360))]

        bot.handle_callback_query(self.callback("dls:token:quality"))

        self.assertEqual(bot.api.callback_answers, [("callback-id", "Choose quality.")])
        chat, message_id, text, markup = bot.api.edits[-1]
        self.assertEqual((message_id, text), (99, "Choose quality:"))
        self.assertEqual(markup["inline_keyboard"][1][0]["callback_data"], "dlq:token:h360")
        self.assertEqual(set(session.options), {"auto", "h360"})
        self.assertEqual(bot.api.messages, [])  # no new message is sent

    def test_cancel_stops_a_running_download_and_forgets_the_session(self) -> None:
        bot, session, _calls = self.bot_with_session(running=True)

        bot.handle_callback_query(self.callback("dls:token:cancel"))

        self.assertTrue(session.cancel_event.is_set())
        self.assertNotIn("token", bot.sessions)
        self.assertEqual(bot.api.edits[-1][1:3], (99, "Download canceled."))
        self.assertEqual(bot.api.callback_answers, [("callback-id", "Canceled.")])

    def test_expired_and_foreign_callbacks_are_rejected_in_the_clickers_language(self) -> None:
        bot = make_bot()
        bot.sessions["old"] = DownloadSession(token="old", url="https://example.test", context=ChatContext(chat_id=-100), requester_id=200, created_at=0)
        bot.sessions["foreign"] = DownloadSession(token="foreign", url="https://example.test", context=ChatContext(chat_id=-100), requester_id=200, created_at=time.time())

        bot.handle_callback_query({"id": "old-cb", "from": {"id": 200}, "data": "dls:old:retry"})
        bot.handle_callback_query({"id": "foreign-cb", "from": {"id": 201}, "data": "dls:foreign:retry"})
        bot.handle_callback_query({"id": "ru-cb", "from": {"id": 201, "language_code": "ru"}, "data": "dls:foreign:retry"})

        self.assertEqual(
            bot.api.callback_answers,
            [
                ("old-cb", "This download expired."),
                ("foreign-cb", "Only the requester can use this download."),
                ("ru-cb", "Только автор запроса может использовать эту кнопку."),
            ],
        )

    def test_unknown_callbacks_are_acknowledged(self) -> None:
        bot = make_bot()
        bot.handle_callback_query({"id": "x", "from": {"id": 1}, "data": "noop"})
        self.assertEqual(bot.api.callback_answers, [("x", "")])


class SoundButtonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.audio = DownloadedMedia(
            paths=(make_media_file(".mp3"),), title="original sound", uploader="Bob",
            webpage_url="https://www.tiktok.com/@bob/video/1", performer="Alice", duration=15, is_audio_only=True,
        )
        self.downloader = FakeDownloader(self.audio)
        self.bot = make_bot(downloader=self.downloader)
        self.bot.extra_requests["tok"] = ExtraRequest(
            url="https://www.tiktok.com/@bob/video/1",
            context=ChatContext(chat_id=-100, requester_id=200, chat_type="supergroup"),
            created_at=time.time(),
        )

    def press(self, *, user_id: int = 300, message_id: int = 55, chat_type: str = "supergroup", lang: str | None = None) -> None:
        sender: dict[str, Any] = {"id": user_id}
        if lang:
            sender["language_code"] = lang
        self.bot.handle_callback_query(
            {
                "id": "cb",
                "from": sender,
                "data": "snd:tok",
                "message": {"message_id": message_id, "chat": {"id": -100, "type": chat_type}},
            }
        )

    def test_pressing_the_button_sends_only_the_audio_and_removes_the_button(self) -> None:
        self.press()

        self.assertTrue(self.downloader.calls[0][1]["audio_only"])
        sent = self.bot.api.media[0]
        self.assertEqual(sent["kind"], "audio")
        self.assertEqual((sent["meta"].title, sent["meta"].performer, sent["meta"].duration), ("original sound", "Alice", 15))
        self.assertEqual(self.bot.api.callback_answers, [("cb", "Getting the sound…")])
        self.assertEqual(self.bot.api.markup_edits, [(-100, 55)])
        self.assertNotIn("tok", self.bot.extra_requests)
        self.assertEqual(self.bot.stats["sounds_sent"], 1)

    def test_helper_message_is_deleted_instead_of_edited(self) -> None:
        self.bot.extra_requests["tok"].is_helper = True

        self.press()

        self.assertEqual(self.bot.api.deletions, [(-100, 55)])
        self.assertEqual(self.bot.api.markup_edits, [])

    def test_anyone_in_a_group_can_use_the_button_but_the_chat_default_reply_setting_is_respected(self) -> None:
        self.press(user_id=999)
        self.assertIsNone(self.bot.api.media[0]["context"].reply_to_message_id)

        self.bot.extra_requests["tok"] = ExtraRequest(url="https://x.test", context=ChatContext(chat_id=-100), created_at=time.time())
        self.bot.settings.toggle(-100, "reply")
        self.press()
        self.assertEqual(self.bot.api.media[1]["context"].reply_to_message_id, 55)
        self.assertTrue(self.bot.api.media[1]["context"].silent)

    def test_second_press_is_served_from_the_file_id_cache(self) -> None:
        self.press()
        self.bot.extra_requests["tok"] = ExtraRequest(url="https://www.tiktok.com/@bob/video/1", context=ChatContext(chat_id=-100), created_at=time.time())
        self.press()

        self.assertEqual(len(self.downloader.calls), 1)
        self.assertEqual(self.bot.api.media[1]["source"], "file-1")

    def test_press_is_ignored_while_the_sound_is_already_being_fetched(self) -> None:
        self.bot.extra_requests["tok"].busy = True

        self.press()

        self.assertEqual(self.downloader.calls, [])
        self.assertEqual(self.bot.api.callback_answers, [("cb", "The sound is already on its way.")])

    def test_expired_buttons_and_unauthorized_private_users(self) -> None:
        self.bot.extra_requests["tok"].created_at = 0
        self.press()
        self.assertEqual(self.bot.api.callback_answers[-1][1], "This download expired.")
        self.assertNotIn("tok", self.bot.extra_requests)

        self.bot.extra_requests["tok"] = ExtraRequest(url="https://x.test", context=ChatContext(chat_id=5), created_at=time.time())
        self.bot.config.authorized_users = frozenset({1})
        self.press(chat_type="private")
        self.assertEqual(self.bot.api.callback_answers[-1][1], "You are not authorized to use this bot.")
        self.assertEqual(self.downloader.calls, [])

    def test_failures_are_reported_and_the_button_stays_usable(self) -> None:
        self.downloader.error = NoSoundError("TikTok post has no downloadable sound")

        self.press(lang="ru")

        self.assertIn("Не удалось получить звук", self.bot.api.messages[-1][1])
        self.assertIn("tok", self.bot.extra_requests)
        self.assertFalse(self.bot.extra_requests["tok"].busy)
        self.assertEqual(self.bot.api.markup_edits, [])

    def test_busy_queue_releases_the_button(self) -> None:
        self.bot.scheduler.accept = False

        self.press()

        self.assertFalse(self.bot.extra_requests["tok"].busy)


class GifButtonTests(unittest.TestCase):
    URL = "https://www.tiktok.com/@bob/video/1"

    def setUp(self) -> None:
        self.video = DownloadedMedia(
            paths=(make_media_file(".mp4"),), title="funny cat", uploader="Bob", webpage_url=self.URL, duration=12
        )
        self.downloader = FakeDownloader(self.video)
        self.bot = make_bot(downloader=self.downloader)
        self.bot.run_download_session(make_session(self.URL))
        markup = self.bot.api.media[0]["reply_markup"]
        self.token = markup["inline_keyboard"][0][0]["callback_data"][4:]
        self.bot.api.media.clear()
        self.gif_file = make_media_file(".mp4", size=7)
        self.converted: list[tuple[Path, Path, int]] = []

        def fake_convert(video: Path, directory: Path, *, max_seconds: int = 60, **_kwargs: Any) -> Path:
            self.converted.append((video, directory, max_seconds))
            return self.gif_file

        patches = [
            mock.patch("tiktok_downloader_bot.bot.convert_to_animation", fake_convert),
            mock.patch("tiktok_downloader_bot.bot.probe_media", lambda _path: {"width": 480, "height": 854, "duration": 12}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def press(self, action: str, *, message_id: int = 55, user_id: int = 300, lang: str | None = None) -> None:
        sender: dict[str, Any] = {"id": user_id}
        if lang:
            sender["language_code"] = lang
        self.bot.handle_callback_query(
            {
                "id": f"cb-{action}",
                "from": sender,
                "data": f"{action}:{self.token}",
                "message": {"message_id": message_id, "chat": {"id": -100, "type": "supergroup"}},
            }
        )

    def test_gif_button_downloads_a_small_copy_converts_and_sends_an_animation(self) -> None:
        self.press("gif")

        kwargs = self.downloader.calls[-1][1]
        self.assertEqual((kwargs["format_selector"], kwargs["max_height"]), (quality_format_selector(480), 480))
        self.assertFalse(kwargs.get("audio_only"))
        self.assertEqual(self.converted[0][0], self.video.paths[0])
        self.assertEqual(self.converted[0][2], 60)
        sent = self.bot.api.media[0]
        self.assertEqual((sent["kind"], sent["source"]), ("animation", self.gif_file))
        self.assertEqual((sent["meta"].width, sent["meta"].height, sent["meta"].duration), (480, 854, 12))
        self.assertTrue(sent["context"].silent)
        self.assertIsNone(sent["context"].reply_to_message_id)
        self.assertIn("Bob", sent["caption"])
        self.assertNotIn("funny cat", sent["caption"])
        self.assertEqual(self.bot.api.callback_answers, [("cb-gif", "Making the GIF…")])
        self.assertEqual(self.bot.stats["gifs_sent"], 1)

    def test_used_button_disappears_but_the_other_one_stays(self) -> None:
        self.press("gif")

        self.assertEqual(button_labels(self.bot.api.markup_values[-1]), ["🎵 Sound"])
        self.assertIn(self.token, self.bot.extra_requests)

        self.press("snd")

        self.assertIsNone(self.bot.api.markup_values[-1])  # keyboard fully removed
        self.assertNotIn(self.token, self.bot.extra_requests)
        self.assertEqual([m["kind"] for m in self.bot.api.media], ["animation", "audio"])

    def test_sound_first_then_gif_also_works(self) -> None:
        self.downloader.media = DownloadedMedia(paths=(make_media_file(".mp3"),), title="s", uploader="Bob", is_audio_only=True)
        self.press("snd")
        self.assertEqual(button_labels(self.bot.api.markup_values[-1]), ["🎞 GIF"])

        self.downloader.media = self.video
        self.press("gif")
        self.assertIsNone(self.bot.api.markup_values[-1])
        self.assertEqual([m["kind"] for m in self.bot.api.media], ["audio", "animation"])

    def test_a_used_button_cannot_be_used_twice(self) -> None:
        self.press("gif")
        calls = len(self.downloader.calls)

        self.press("gif")

        self.assertEqual(len(self.downloader.calls), calls)
        self.assertEqual(self.bot.api.callback_answers[-1], ("cb-gif", "This download expired."))

    def test_second_gif_is_served_from_the_file_id_cache(self) -> None:
        self.press("gif")
        self.bot.extra_requests[self.token] = ExtraRequest(
            url=self.URL, context=ChatContext(chat_id=-100), created_at=time.time(), actions=["gif"]
        )
        calls = len(self.downloader.calls)

        self.press("gif")

        self.assertEqual(len(self.downloader.calls), calls)
        self.assertEqual(self.bot.api.media[1]["source"], "file-2")  # file-1 was the original video
        self.assertEqual(self.bot.api.media[1]["kind"], "animation")
        self.assertEqual(len(self.converted), 1)

    def test_busy_requests_are_not_started_twice(self) -> None:
        self.bot.extra_requests[self.token].busy = True

        self.press("gif")

        self.assertEqual(self.converted, [])
        self.assertEqual(self.bot.api.callback_answers[-1], ("cb-gif", "The GIF is already being made."))

    def test_pressing_the_other_button_while_busy_says_so_instead_of_blaming_the_wrong_action(self) -> None:
        request = self.bot.extra_requests[self.token]
        request.busy = True
        request.busy_action = "gif"

        calls = len(self.downloader.calls)

        self.press("snd")

        self.assertEqual(len(self.downloader.calls), calls)
        self.assertEqual(
            self.bot.api.callback_answers[-1],
            ("cb-snd", "The other button of this message is still being processed. Please wait a moment."),
        )
        self.press("gif")
        self.assertEqual(self.bot.api.callback_answers[-1], ("cb-gif", "The GIF is already being made."))

    def test_conversion_failures_are_reported_and_the_button_stays(self) -> None:
        def broken(*_args: Any, **_kwargs: Any) -> Path:
            raise RuntimeError("ffmpeg could not convert the video to a GIF: boom")

        with mock.patch("tiktok_downloader_bot.bot.convert_to_animation", broken):
            self.bot.config.admin_users = frozenset({1})
            self.press("gif", lang="ru")

        user_messages = [m for m in self.bot.api.messages if m[0] == -100 and "GIF" in m[1]]
        self.assertTrue(user_messages[-1][1].startswith("Не удалось сделать GIF"))
        self.assertEqual(self.bot.api.markup_values, [])  # keyboard untouched: both buttons stay
        self.assertEqual(self.bot.extra_requests[self.token].actions, ["sound", "gif"])
        self.assertFalse(self.bot.extra_requests[self.token].busy)
        self.assertEqual(self.bot.api.media, [])
        self.assertTrue(any(m[0] == 1 for m in self.bot.api.messages))  # unexpected -> admin notified

    def test_oversized_gifs_are_rejected_before_upload(self) -> None:
        self.bot.config.max_upload_bytes = 3

        self.press("gif")

        self.assertEqual(self.bot.api.media, [])
        self.assertIn("too large", self.bot.api.messages[-1][1])

    def test_non_video_downloads_are_not_converted(self) -> None:
        self.downloader.media = DownloadedMedia(paths=(make_media_file(".jpg"),), title="p")

        self.press("gif")

        self.assertEqual(self.converted, [])
        self.assertEqual(self.bot.api.media, [])

    def test_unauthorized_private_users_cannot_press_it(self) -> None:
        self.bot.config.authorized_users = frozenset({1})
        self.bot.handle_callback_query(
            {
                "id": "cb",
                "from": {"id": 5},
                "data": f"gif:{self.token}",
                "message": {"message_id": 55, "chat": {"id": 5, "type": "private"}},
            }
        )

        self.assertEqual(self.converted, [])
        self.assertEqual(self.bot.api.callback_answers[-1][1], "You are not authorized to use this bot.")


class SettingsTests(unittest.TestCase):
    def callback(self, data: str, user_id: int, chat_type: str = "supergroup") -> dict[str, Any]:
        return {
            "id": "cb",
            "from": {"id": user_id},
            "data": data,
            "message": {"message_id": 9, "chat": {"id": -100, "type": chat_type}},
        }

    def test_settings_command_shows_toggles_for_private_chats(self) -> None:
        bot = make_bot()

        bot.handle_message(message("/settings"))

        text, markup = bot.api.messages[-1][1], bot.api.messages[-1][3]
        self.assertTrue(text.startswith("Settings for this chat"))
        callbacks = [row[0]["callback_data"] for row in markup["inline_keyboard"]]
        self.assertEqual(callbacks, ["cfg:quality", "cfg:captions", "cfg:sound", "cfg:gif", "cfg:delete_original", "cfg:reply"])

    def test_group_settings_include_auto_download_and_require_admin(self) -> None:
        bot = make_bot()
        bot.api.chat_members[(-100, 1)] = "administrator"

        bot.handle_message(message("/settings", chat_id=-100, chat_type="supergroup", user_id=2))
        self.assertEqual(bot.api.messages[-1][1], "Only chat admins can change the settings.")

        bot.handle_message(message("/settings", chat_id=-100, chat_type="supergroup", user_id=1))
        callbacks = [row[0]["callback_data"] for row in bot.api.messages[-1][3]["inline_keyboard"]]
        self.assertIn("cfg:auto", callbacks)

    def test_toggle_callback_updates_the_store_and_redraws(self) -> None:
        bot = make_bot()
        bot.api.chat_members[(-100, 1)] = "creator"

        bot.handle_callback_query(self.callback("cfg:captions", 1))
        bot.handle_callback_query(self.callback("cfg:quality", 1))

        self.assertFalse(bot.settings.get(-100)["captions"])
        self.assertEqual(bot.settings.get(-100)["quality"], "auto")
        chat, message_id, text, markup = bot.api.edits[-1]
        self.assertEqual(message_id, 9)
        self.assertIn("Default quality: best that fits", markup["inline_keyboard"][0][0]["text"])
        self.assertIn("Captions: off", markup["inline_keyboard"][1][0]["text"])
        self.assertEqual(bot.api.callback_answers[-1], ("cb", "Saved."))

    def test_non_admins_cannot_toggle(self) -> None:
        bot = make_bot()

        bot.handle_callback_query(self.callback("cfg:captions", 2))

        self.assertTrue(bot.settings.get(-100)["captions"])
        self.assertEqual(bot.api.edits, [])
        self.assertEqual(bot.api.callback_answers[-1][1], "Only chat admins can change the settings.")

    def test_allowlist_decides_who_manages_settings_when_configured(self) -> None:
        bot = make_bot(authorized=frozenset({7}))

        bot.handle_callback_query(self.callback("cfg:sound", 7))
        bot.handle_callback_query(self.callback("cfg:reply", 8))

        self.assertFalse(bot.settings.get(-100)["sound"])
        self.assertFalse(bot.settings.get(-100)["reply"])

    def test_unknown_setting_keys_are_rejected(self) -> None:
        bot = make_bot()
        bot.handle_callback_query(self.callback("cfg:hack", 5, chat_type="private"))
        self.assertEqual(bot.api.callback_answers[-1][1], "Unknown download action.")

    def test_keyboard_is_localized(self) -> None:
        keyboard = settings_keyboard("ru", SettingsStore(None).get(1), group=False)
        self.assertEqual(keyboard["inline_keyboard"][1][0]["text"], "Подписи: вкл")


class InlineModeTests(unittest.TestCase):
    URL = "https://www.tiktok.com/@bob/video/1"

    def test_uncached_links_offer_a_download_article_with_a_placeholder_keyboard(self) -> None:
        bot = make_bot(cache_chat_id="-1001")

        bot.handle_inline_query({"id": "q1", "from": {"id": 5}, "query": f"look {self.URL}"})

        query_id, results = bot.api.inline_answers[-1]
        self.assertEqual(query_id, "q1")
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["id"].startswith("dl:"))
        self.assertIn("reply_markup", results[0])

    def test_cached_links_are_answered_with_the_cached_file(self) -> None:
        bot = make_bot(cache_chat_id="-1001")
        bot.file_cache.put(cache_key("default", self.URL), {"items": [["video", "vid-1"]], "title": "cat", "uploader": "Bob", "url": self.URL})

        bot.handle_inline_query({"id": "q1", "from": {"id": 5}, "query": self.URL})

        result = bot.api.inline_answers[-1][1][0]
        self.assertEqual((result["type"], result["video_file_id"]), ("video", "vid-1"))
        self.assertIn("Bob", result["caption"])

    def test_queries_without_links_get_no_results_and_missing_cache_chat_is_explained(self) -> None:
        bot = make_bot(cache_chat_id="-1001")
        bot.handle_inline_query({"id": "q1", "from": {"id": 5}, "query": "hello"})
        self.assertEqual(bot.api.inline_answers[-1][1], [])

        unconfigured = make_bot()
        unconfigured.handle_inline_query({"id": "q2", "from": {"id": 5}, "query": self.URL})
        self.assertEqual(unconfigured.api.inline_answers[-1][1][0]["title"], "Inline mode is not set up")

    def test_unauthorized_users_are_turned_away(self) -> None:
        bot = make_bot(cache_chat_id="-1001", authorized=frozenset({1}))
        bot.handle_inline_query({"id": "q1", "from": {"id": 5}, "query": self.URL})
        self.assertEqual(bot.api.inline_answers[-1][1][0]["title"], "Not authorized")

    def test_chosen_result_downloads_caches_and_swaps_the_media_into_the_message(self) -> None:
        media = DownloadedMedia(paths=(make_media_file(".mp4"),), title="cat", uploader="Bob", webpage_url=self.URL)
        bot = make_bot(cache_chat_id="-1001", downloader=FakeDownloader(media))

        bot.handle_chosen_inline_result(
            {"result_id": "dl:abc", "from": {"id": 5}, "query": self.URL, "inline_message_id": "inl-1"}
        )

        self.assertEqual(str(bot.api.media[0]["context"].chat_id), "-1001")
        inline_id, edit = bot.api.inline_media_edits[-1]
        self.assertEqual((inline_id, edit["type"], edit["media"]), ("inl-1", "video", "file-1"))
        self.assertIn("Bob", edit["caption"])
        self.assertIsNotNone(bot.file_cache.get(cache_key("default", self.URL)))

    def test_chosen_result_failures_and_albums_edit_the_message_text(self) -> None:
        failing = make_bot(cache_chat_id="-1001", downloader=FakeDownloader(error=RuntimeError("Private video")))
        failing.handle_chosen_inline_result({"result_id": "dl:abc", "from": {"id": 5}, "query": self.URL, "inline_message_id": "inl-1"})
        self.assertEqual(failing.api.inline_text_edits[-1], ("inl-1", "Download failed: Media is unavailable or private."))

        album = DownloadedMedia(paths=tuple(make_media_file(".jpg") for _ in range(3)), title="p")
        bot = make_bot(cache_chat_id="-1001", downloader=FakeDownloader(album))
        bot.handle_chosen_inline_result({"result_id": "dl:abc", "from": {"id": 5}, "query": self.URL, "inline_message_id": "inl-2"})
        self.assertIn("3 items", bot.api.inline_text_edits[-1][1])

    def test_chosen_results_from_other_sources_are_ignored(self) -> None:
        bot = make_bot(cache_chat_id="-1001", downloader=FakeDownloader(error=RuntimeError("x")))
        bot.handle_chosen_inline_result({"result_id": "other", "from": {"id": 5}, "query": self.URL, "inline_message_id": "i"})
        bot.handle_chosen_inline_result({"result_id": "dl:1", "from": {"id": 5}, "query": self.URL})
        self.assertEqual(bot.downloader.calls, [])


class LifecycleTests(unittest.TestCase):
    def test_first_signal_interrupts_a_blocking_poll_only_while_polling(self) -> None:
        bot = make_bot()
        bot.polling = False
        bot.stop()
        self.assertFalse(bot.running)

        polling = make_bot()
        polling.polling = True
        with self.assertRaises(StopPolling):
            polling.stop()
        self.assertFalse(polling.running)

    def test_prune_drops_expired_idle_sessions_and_sound_buttons_but_keeps_running_ones(self) -> None:
        bot = make_bot()
        old = make_session()
        old.created_at = 0
        busy = make_session()
        busy.created_at = 0
        busy.running = True
        fresh = make_session()
        bot.sessions.update({"old": old, "busy": busy, "fresh": fresh})
        bot.extra_requests["old"] = ExtraRequest(url="u", context=ChatContext(chat_id=1), created_at=0)
        bot.extra_requests["new"] = ExtraRequest(url="u", context=ChatContext(chat_id=1), created_at=time.time())

        bot.prune()

        self.assertEqual(set(bot.sessions), {"busy", "fresh"})
        self.assertEqual(set(bot.extra_requests), {"new"})

    def test_help_mentions_inline_only_when_it_is_configured(self) -> None:
        self.assertNotIn("@media_bot", help_text("en", "media_bot", False))
        self.assertIn("@media_bot", help_text("en", "media_bot", True))
        self.assertIn("🎵", help_text("uk"))


if __name__ == "__main__":
    unittest.main()
