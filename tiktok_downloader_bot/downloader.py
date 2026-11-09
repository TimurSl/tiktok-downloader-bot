"""yt-dlp / TikTok downloading with fallbacks, progress and size checks."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

import requests
import yt_dlp

from .config import effective_upload_limit_bytes
from .errors import DownloadCancelled, NoSoundError, TooLargeError
from .media import (
    ALBUM_MEDIA_KINDS,
    AUDIO_EXTENSIONS,
    detect_media_kind,
    extension_from_response,
    find_downloaded_files,
    normalize_url,
    select_downloaded_media,
)
from .tiktok import (
    TIKTOK_WEB_HEADERS,
    extract_tiktok_author,
    extract_tiktok_image_urls,
    extract_tiktok_item_struct,
    extract_tiktok_music,
    extract_tiktok_rehydration_data,
)
from .urls import is_tiktok_photo_url, is_tiktok_redirect_url, is_tiktok_url, tiktok_photo_id

LOG = logging.getLogger("tiktok_downloader_bot")

YOUTUBE_QUALITY_HEIGHTS = (1080, 720, 480, 360)
# An estimated size must exceed the limit by this factor before a download is aborted.
ESTIMATE_TOLERANCE = 1.15
THUMBNAIL_MAX_BYTES = 200 * 1024


@dataclass
class DownloadedMedia:
    paths: tuple[Path, ...]
    title: str
    uploader: str = ""
    webpage_url: str = ""
    duration: int | None = None
    width: int | None = None
    height: int | None = None
    performer: str = ""
    thumbnail: Path | None = None
    is_audio_only: bool = False

    @property
    def path(self) -> Path:
        return self.paths[0]

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(detect_media_kind(path) for path in self.paths)

    @property
    def is_album(self) -> bool:
        return len(self.paths) > 1 and all(kind in ALBUM_MEDIA_KINDS for kind in self.kinds)


@dataclass(frozen=True)
class QualityOption:
    key: str
    label: str
    format_selector: str | None
    approx_bytes: int | None = None
    max_height: int | None = None
    audio_only: bool = False


@dataclass(frozen=True)
class DownloadStrategy:
    name: str
    format_selector: str


@dataclass(frozen=True)
class ProgressInfo:
    stage: str  # "downloading" | "processing"
    percent: float | None = None
    speed: float | None = None


ProgressCallback = Callable[[ProgressInfo], None]


@dataclass
class AttemptState:
    totals: dict[str, tuple[float, bool]] = field(default_factory=dict)
    too_large: bool = False
    cancelled: bool = False


class MediaDownloader:
    def __init__(self, config: Any) -> None:
        self.config = config
        config.download_dir.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()

    @property
    def session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._local.session = requests.Session()
        return session

    def max_bytes(self) -> int:
        return effective_upload_limit_bytes(self.config)

    def cleanup_stale_jobs(self) -> None:
        """Remove leftovers of jobs that were interrupted by a crash or restart."""
        for pattern in ("job-*", "ytdlp-quality-*"):
            for path in self.config.download_dir.glob(pattern):
                shutil.rmtree(path, ignore_errors=True)

    @contextmanager
    def download(
        self,
        url: str,
        *,
        format_selector: str | None = None,
        max_height: int | None = None,
        audio_only: bool = False,
        progress: ProgressCallback | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> Iterator[DownloadedMedia]:
        job_dir = Path(tempfile.mkdtemp(prefix="job-", dir=self.config.download_dir))
        try:
            download_url = self.resolve_tiktok_url(url)
            if is_tiktok_photo_url(download_url):
                if audio_only:
                    media = self.download_tiktok_sound(download_url, job_dir)
                else:
                    media = self.download_tiktok_photo_post(download_url, job_dir)
            else:
                media = self.download_with_ytdlp(
                    download_url,
                    job_dir,
                    format_selector=format_selector,
                    max_height=max_height,
                    audio_only=audio_only,
                    progress=progress,
                    cancel=cancel,
                )
            yield media
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    def download_with_ytdlp(
        self,
        download_url: str,
        job_dir: Path,
        *,
        format_selector: str | None,
        max_height: int | None,
        audio_only: bool,
        progress: ProgressCallback | None,
        cancel: Callable[[], bool] | None,
    ) -> DownloadedMedia:
        failures: list[str] = []
        errors: list[BaseException] = []
        fallback_attempted = False

        for index, strategy in enumerate(
            self.download_strategies(format_selector, max_height=max_height, audio_only=audio_only), start=1
        ):
            if cancel and cancel():
                raise DownloadCancelled("canceled")
            attempt_dir = job_dir / f"{index:02d}-{strategy.name}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            try:
                return self.run_ytdlp(
                    download_url,
                    attempt_dir,
                    strategy,
                    audio_only=audio_only,
                    progress=progress,
                    cancel=cancel,
                )
            except DownloadCancelled:
                raise
            except Exception as exc:
                errors.append(exc)
                failures.append(f"{strategy.name}: {exc}")
                LOG.debug("yt-dlp strategy %s failed for %s: %s", strategy.name, download_url, exc)
                if is_tiktok_url(download_url) and not fallback_attempted and not isinstance(exc, TooLargeError):
                    fallback_attempted = True
                    try:
                        if audio_only:
                            return self.download_tiktok_sound(download_url, job_dir)
                        return self.download_tiktok_photo_post(download_url, job_dir)
                    except Exception as fallback_exc:
                        LOG.debug("TikTok fallback failed for %s: %s", download_url, fallback_exc)

        failure_summary = "; ".join(failures[-3:])
        if errors and all(isinstance(error, TooLargeError) for error in errors):
            raise TooLargeError(f"file is larger than the max upload of {self.max_bytes() // 1024 // 1024} MB")
        raise RuntimeError(
            f"yt-dlp could not download media after {len(failures)} strategies: {failure_summary}"
        ) from (errors[-1] if errors else None)

    def run_ytdlp(
        self,
        download_url: str,
        attempt_dir: Path,
        strategy: DownloadStrategy,
        *,
        audio_only: bool,
        progress: ProgressCallback | None,
        cancel: Callable[[], bool] | None,
    ) -> DownloadedMedia:
        state = AttemptState()
        options = self.ytdlp_options(
            attempt_dir,
            strategy.format_selector,
            audio_only=audio_only,
            progress_hook=self.make_progress_hook(state, progress, cancel),
            postprocessor_hook=self.make_postprocessor_hook(progress),
        )
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(download_url, download=True)
        except Exception as exc:
            # yt-dlp may wrap exceptions raised by our hooks, so trust the flags we set.
            if state.cancelled:
                raise DownloadCancelled("canceled") from exc
            if state.too_large:
                raise TooLargeError("file is larger than the max upload size") from exc
            raise

        downloaded_files = find_downloaded_files(attempt_dir)
        if not downloaded_files:
            raise RuntimeError("yt-dlp completed without a usable output file")
        media_paths = select_downloaded_media(downloaded_files)
        if not media_paths:
            raise RuntimeError("yt-dlp completed without a usable media output file")
        return self.build_media(info, media_paths, download_url, attempt_dir.parent, audio_only=audio_only)

    def build_media(
        self,
        info: Any,
        media_paths: tuple[Path, ...],
        download_url: str,
        job_dir: Path,
        *,
        audio_only: bool,
    ) -> DownloadedMedia:
        entry = first_entry(info)
        uploader = str(entry.get("uploader") or entry.get("channel") or entry.get("creator") or "")
        title = str(entry.get("title") or media_paths[0].stem)
        performer = ""
        if audio_only:
            title = str(entry.get("track") or title)
            artists = entry.get("artists")
            performer = str(
                entry.get("artist")
                or (", ".join(str(a) for a in artists) if isinstance(artists, list) else "")
                or uploader
            )
        media = DownloadedMedia(
            paths=media_paths,
            title=title,
            uploader=uploader,
            webpage_url=str(entry.get("webpage_url") or download_url),
            performer=performer,
            is_audio_only=audio_only,
            duration=positive_int(entry.get("duration")),
        )
        if len(media_paths) == 1:
            kind = detect_media_kind(media_paths[0])
            if kind == "video":
                media.width = positive_int(entry.get("width"))
                media.height = positive_int(entry.get("height"))
                if not (media.width and media.height and media.duration):
                    probed = probe_media(media_paths[0])
                    media.width = media.width or probed.get("width")
                    media.height = media.height or probed.get("height")
                    media.duration = media.duration or probed.get("duration")
                media.thumbnail = make_thumbnail(media_paths[0], job_dir / "thumbs")
            elif kind == "audio" and not media.duration:
                media.duration = probe_media(media_paths[0]).get("duration")
        return media

    def make_progress_hook(
        self,
        state: AttemptState,
        progress: ProgressCallback | None,
        cancel: Callable[[], bool] | None,
    ) -> Callable[[dict[str, Any]], None]:
        limit = self.max_bytes()

        def hook(data: dict[str, Any]) -> None:
            if cancel and cancel():
                state.cancelled = True
                raise DownloadCancelled("canceled")
            if data.get("status") != "downloading":
                return

            total = data.get("total_bytes")
            estimated = False
            if not total:
                total = data.get("total_bytes_estimate")
                estimated = True
            if total:
                state.totals[str(data.get("filename") or data.get("tmpfilename") or "?")] = (float(total), estimated)
                projected = sum(
                    size / ESTIMATE_TOLERANCE if is_estimate else size
                    for size, is_estimate in state.totals.values()
                )
                if projected > limit:
                    state.too_large = True
                    raise TooLargeError("file is larger than the max upload size")

            if progress:
                downloaded = data.get("downloaded_bytes")
                percent = float(downloaded) / float(total) * 100 if total and downloaded else None
                speed = data.get("speed")
                progress(ProgressInfo("downloading", percent, float(speed) if speed else None))

        return hook

    @staticmethod
    def make_postprocessor_hook(progress: ProgressCallback | None) -> Callable[[dict[str, Any]], None]:
        def hook(data: dict[str, Any]) -> None:
            if progress and data.get("status") == "started":
                progress(ProgressInfo("processing"))

        return hook

    def download_strategies(
        self,
        format_selector: str | None,
        *,
        max_height: int | None = None,
        audio_only: bool = False,
    ) -> list[DownloadStrategy]:
        if audio_only:
            candidates = [
                DownloadStrategy("audio", format_selector or "bestaudio/best"),
                DownloadStrategy("audio-any", "best"),
            ]
        else:
            requested = format_selector or self.config.ytdlp_format
            cap = f"[height<={max_height}]" if max_height else ""
            candidates = [
                DownloadStrategy("requested", requested),
                DownloadStrategy(
                    "best-merged",
                    f"bv*{cap}+ba/b{cap}" if cap else "bestvideo*+bestaudio/bestvideo+bestaudio/best",
                ),
                DownloadStrategy("compatible-mp4", f"bv*{cap}[ext=mp4]+ba[ext=m4a]/b{cap}[ext=mp4]/b{cap}"),
                DownloadStrategy("single-best", f"b{cap}" if cap else "best"),
            ]
            if not max_height or max_height > 480:
                candidates.append(DownloadStrategy("smaller", "bv*[height<=480]+ba/b[height<=480]/worst"))

        strategies: list[DownloadStrategy] = []
        seen: set[str] = set()
        for strategy in candidates:
            if not strategy.format_selector or strategy.format_selector in seen:
                continue
            seen.add(strategy.format_selector)
            strategies.append(strategy)
        return strategies

    def ytdlp_options(
        self,
        job_dir: Path,
        format_selector: str,
        *,
        audio_only: bool = False,
        progress_hook: Callable[[dict[str, Any]], None] | None = None,
        postprocessor_hook: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        options: dict[str, Any] = {
            "format": format_selector,
            "outtmpl": str(job_dir / "%(title).180B-%(id)s.%(ext)s"),
            "noplaylist": self.config.ytdlp_no_playlist,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "restrictfilenames": True,
            "continuedl": True,
            "file_access_retries": 3,
            "fragment_retries": 3,
            "retries": 3,
            "socket_timeout": 30,
        }
        if audio_only:
            options["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
            ]
        else:
            options["merge_output_format"] = "mp4"
            options["postprocessor_args"] = {"merger": ["-movflags", "+faststart"]}
        if progress_hook:
            options["progress_hooks"] = [progress_hook]
        if postprocessor_hook:
            options["postprocessor_hooks"] = [postprocessor_hook]
        options.update(self.cookie_options(job_dir))
        return options

    def cookie_options(self, directory: Path) -> dict[str, Any]:
        options: dict[str, Any] = {}
        if self.config.ytdlp_cookies:
            cookie_copy = directory / "cookies.txt"
            shutil.copyfile(self.config.ytdlp_cookies, cookie_copy)
            cookie_copy.chmod(0o600)
            options["cookiefile"] = str(cookie_copy)
        if self.config.ytdlp_cookies_from_browser:
            options["cookiesfrombrowser"] = tuple(self.config.ytdlp_cookies_from_browser.split(":"))
        return options

    def youtube_quality_options(self, url: str) -> list[QualityOption]:
        with tempfile.TemporaryDirectory(prefix="ytdlp-quality-", dir=self.config.download_dir) as tmp_dir:
            options: dict[str, Any] = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": self.config.ytdlp_no_playlist,
                "skip_download": True,
                **self.cookie_options(Path(tmp_dir)),
            }
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)

        entry = first_entry(info)
        formats = entry.get("formats")
        if not isinstance(formats, list):
            formats = []
        duration = float(entry.get("duration") or 0)
        audio = best_audio_format(formats)
        limit = self.max_bytes()

        fitting: list[QualityOption] = []
        oversized: list[QualityOption] = []
        seen_heights: set[int] = set()
        for height in YOUTUBE_QUALITY_HEIGHTS:
            video = best_video_format(formats, max_height=height)
            if not video:
                continue
            selected_height = int(video.get("height") or height)
            if selected_height in seen_heights:
                continue
            seen_heights.add(selected_height)
            approx_bytes = combined_format_size(video, audio, duration)
            label = f"{selected_height}p"
            if approx_bytes:
                label = f"{label} ~{format_bytes(approx_bytes)}"
            option = QualityOption(
                key=f"h{selected_height}",
                label=label,
                format_selector=quality_format_selector(selected_height),
                approx_bytes=approx_bytes,
                max_height=selected_height,
            )
            (oversized if approx_bytes and approx_bytes > limit else fitting).append(option)

        if not fitting and oversized:
            # Nothing is known to fit: still offer the smallest one, flagged.
            smallest = oversized[-1]
            fitting.append(
                QualityOption(
                    smallest.key, f"{smallest.label} ⚠", smallest.format_selector, smallest.approx_bytes, smallest.max_height
                )
            )

        best = fitting[0] if fitting else None
        auto = QualityOption(
            "auto",
            "Auto",
            best.format_selector if best else None,
            best.approx_bytes if best else None,
            best.max_height if best else None,
        )
        audio_option = QualityOption("audio", "Audio (MP3)", "bestaudio/best", audio_only=True)
        return [auto, *fitting, audio_option]

    def resolve_tiktok_url(self, url: str) -> str:
        if not is_tiktok_url(url) or not is_tiktok_redirect_url(url):
            return url

        try:
            with self.session.get(
                url,
                headers=TIKTOK_WEB_HEADERS,
                allow_redirects=True,
                stream=True,
                timeout=30,
            ) as response:
                response.raise_for_status()
                return response.url or url
        except requests.RequestException as exc:
            LOG.debug("Could not resolve TikTok redirect URL %s: %s", url, exc)
            return url

    def fetch_tiktok_item(self, url: str) -> dict[str, Any]:
        response = self.session.get(url, headers=TIKTOK_WEB_HEADERS, timeout=30)
        response.raise_for_status()
        data = extract_tiktok_rehydration_data(response.text)
        if not data:
            raise RuntimeError("TikTok post did not expose readable page metadata")
        return extract_tiktok_item_struct(data)

    def download_tiktok_photo_post(self, url: str, job_dir: Path) -> DownloadedMedia:
        item = self.fetch_tiktok_item(url)
        image_urls = extract_tiktok_image_urls(item)
        if not image_urls:
            raise RuntimeError("TikTok photo post did not expose image URLs")

        title = str(item.get("desc") or f"TikTok photo post {tiktok_photo_id(url) or ''}").strip()
        paths = tuple(
            self.download_tiktok_image(image_url, job_dir, index=index, referer=url)
            for index, image_url in enumerate(image_urls, start=1)
        )
        return DownloadedMedia(paths=paths, title=title, uploader=extract_tiktok_author(item), webpage_url=url)

    def download_tiktok_image(self, url: str, job_dir: Path, *, index: int, referer: str) -> Path:
        response = self.session.get(
            normalize_url(url),
            headers={**TIKTOK_WEB_HEADERS, "Referer": referer},
            stream=True,
            timeout=60,
        )
        response.raise_for_status()

        extension = extension_from_response(url, response.headers.get("Content-Type"))
        path = job_dir / f"tiktok-photo-{index:03d}{extension}"
        with path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if chunk:
                    handle.write(chunk)
        return path

    def download_tiktok_sound(self, url: str, job_dir: Path) -> DownloadedMedia:
        """The sound of a TikTok photo post (yt-dlp cannot extract those)."""
        item = self.fetch_tiktok_item(url)
        music = extract_tiktok_music(item)
        if not music:
            raise NoSoundError("TikTok post has no downloadable sound")

        response = self.session.get(
            normalize_url(music.url),
            headers={**TIKTOK_WEB_HEADERS, "Referer": url},
            stream=True,
            timeout=60,
        )
        response.raise_for_status()
        extension = extension_from_response(
            music.url, response.headers.get("Content-Type"), allowed=AUDIO_EXTENSIONS, default=".mp3"
        )
        path = job_dir / f"tiktok-sound{extension}"
        with path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if chunk:
                    handle.write(chunk)
        uploader = extract_tiktok_author(item)
        return DownloadedMedia(
            paths=(path,),
            title=music.title,
            uploader=uploader,
            webpage_url=url,
            performer=music.author or uploader,
            is_audio_only=True,
        )


def first_entry(info: Any) -> dict[str, Any]:
    """The info dict of a single video, or of the first item of a playlist."""
    if not isinstance(info, dict):
        return {}
    entries = info.get("entries")
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict):
                return entry
    return info


def positive_int(value: Any) -> int | None:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def probe_media(path: Path) -> dict[str, int | None]:
    """Width, height and duration via ffprobe (empty values when unavailable)."""
    result: dict[str, int | None] = {"width": None, "height": None, "duration": None}
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return result
    try:
        output = subprocess.run(
            [
                ffprobe, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height:format=duration", "-of", "json", str(path),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        ).stdout
        data = json.loads(output)
    except (OSError, subprocess.SubprocessError, ValueError):
        return result
    streams = data.get("streams") or [{}]
    result["width"] = positive_int(streams[0].get("width"))
    result["height"] = positive_int(streams[0].get("height"))
    result["duration"] = positive_int(round(float((data.get("format") or {}).get("duration") or 0)))
    return result


def make_thumbnail(video: Path, directory: Path) -> Path | None:
    """Best-effort JPEG thumbnail (<=320px, <=200 KB) so Telegram shows a proper preview."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / f"{video.stem[:60]}.jpg"
    for seek in ("1", "0"):
        try:
            subprocess.run(
                [
                    ffmpeg, "-y", "-loglevel", "error", "-ss", seek, "-i", str(video),
                    "-frames:v", "1", "-vf", "scale=320:320:force_original_aspect_ratio=decrease",
                    "-q:v", "6", str(output),
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if output.exists() and 0 < output.stat().st_size <= THUMBNAIL_MAX_BYTES:
            return output
    return None


def convert_to_animation(
    video: Path,
    directory: Path,
    *,
    max_width: int = 480,
    fps: int = 20,
    max_seconds: int = 60,
) -> Path:
    """Convert a video to a silent, looping-friendly H.264 clip.

    Telegram shows such a clip (sent with sendAnimation) as a GIF, at a fraction of the
    size of a real .gif file.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed, cannot make a GIF")
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / f"{video.stem[:60]}-gif.mp4"
    command = [
        ffmpeg, "-y", "-loglevel", "error", "-i", str(video), "-t", str(max_seconds), "-an",
        "-vf", f"fps={fps},scale='trunc(min({max_width},iw)/2)*2':-2",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(output),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=300)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode(errors="replace").strip()[-300:] if exc.stderr else ""
        raise RuntimeError(f"ffmpeg could not convert the video to a GIF: {detail}") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"ffmpeg could not convert the video to a GIF: {exc}") from exc
    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("ffmpeg produced no GIF output")
    return output


def quality_format_selector(height: int) -> str:
    return f"bv*[height<={height}]+ba/b[height<={height}]/best"


def best_video_format(formats: list[Any], *, max_height: int) -> dict[str, Any] | None:
    videos = [
        fmt
        for fmt in formats
        if isinstance(fmt, dict)
        and fmt.get("vcodec") not in {None, "none"}
        and fmt.get("height")
        and int(fmt["height"]) <= max_height
        and not is_storyboard_format(fmt)
    ]
    if not videos:
        return None
    return max(videos, key=lambda fmt: (int(fmt.get("height") or 0), float(fmt.get("tbr") or 0)))


def best_audio_format(formats: list[Any]) -> dict[str, Any] | None:
    audios = [
        fmt
        for fmt in formats
        if isinstance(fmt, dict)
        and fmt.get("acodec") not in {None, "none"}
        and fmt.get("vcodec") in {None, "none"}
    ]
    if not audios:
        return None
    return max(audios, key=lambda fmt: float(fmt.get("abr") or fmt.get("tbr") or 0))


def is_storyboard_format(fmt: dict[str, Any]) -> bool:
    protocol = str(fmt.get("protocol") or "").lower()
    format_note = str(fmt.get("format_note") or "").lower()
    return "storyboard" in protocol or "storyboard" in format_note


def combined_format_size(
    video: dict[str, Any],
    audio: dict[str, Any] | None,
    duration_seconds: float,
) -> int | None:
    video_size = format_size_bytes(video, duration_seconds)
    audio_size = format_size_bytes(audio, duration_seconds) if audio else None
    if video_size and audio_size and video.get("acodec") in {None, "none"}:
        return video_size + audio_size
    return video_size or audio_size


def format_size_bytes(fmt: dict[str, Any] | None, duration_seconds: float) -> int | None:
    if not fmt:
        return None
    for key in ("filesize", "filesize_approx"):
        value = fmt.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    bitrate_kbps = fmt.get("tbr") or fmt.get("vbr") or fmt.get("abr")
    if isinstance(bitrate_kbps, (int, float)) and bitrate_kbps > 0 and duration_seconds > 0:
        return int(duration_seconds * bitrate_kbps * 1000 / 8)
    return None


def format_bytes(size: int) -> str:
    mb = size / 1024 / 1024
    if mb < 1024:
        return f"{mb:.0f} MB"
    return f"{mb / 1024:.1f} GB"
