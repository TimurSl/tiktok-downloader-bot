# TikTok Downloader Bot

Telegram bot that downloads media from TikTok, YouTube, Instagram, X/Twitter, Reddit, Twitch, Vimeo, SoundCloud and other `yt-dlp` sites and sends it back to the chat.

## Features

- **TikTok videos, photo posts and short links.** Photo posts are sent as albums (10 images per album message).
- **Sound on demand.** Videos and TikTok photo posts get a **🎵 Sound** button. The sound is never sent unless somebody presses it. Albums cannot carry buttons, so they get a small silent follow-up message with the button.
- **GIF on demand.** Short videos (up to 60 s) also get a **🎞 GIF** button next to the sound button. Pressing it fetches a small copy of the video, converts it with ffmpeg to a silent looping clip (max 480 px wide) and sends it as a Telegram animation, which Telegram shows and saves as a GIF. Each button disappears once it was used; the other stays.
- **Quiet by default.** The bot never replies to (and so never pings) the user: everything is sent as a silent message. Replies can be switched on per chat in `/settings`, and are silent too.
- **YouTube quality picker** with MP3, retry, reselect and cancel buttons. Qualities that cannot fit into the upload limit are hidden, and "Auto" picks the best one that fits.
- **Live progress** in the status message (`Downloading… 42% · 3.1 MB/s`) and a **Cancel** button that really stops a running download.
- **Concurrent downloads.** Several people can download at once; long downloads never block buttons or other chats. A fair per-user limit keeps one user from taking every slot.
- **Several links per message** (up to `BOT_MAX_LINKS`), each downloaded separately.
- **Instant repeats.** Telegram `file_id`s are cached, so the same link (with or without tracking parameters) is re-sent without downloading again.
- **Proper video uploads**: streaming flag, width/height/duration and a thumbnail. Photo + video carousels are sent as one mixed album.
- **Captions** with the description (trailing hashtag spam removed, long text collapsed), the uploader and a link to the original post.
- **Too-big files are detected early**, while downloading, instead of after downloading everything.
- **`/settings` per chat**: default quality, captions, sound button, GIF button, auto-download in groups, delete the link message afterwards, reply to the link message.
- **`/audio <link>`** downloads only the sound of any supported link (or reply to a message with a link). **`/dl <link>`** downloads a link even where auto-download is off.
- **Inline mode** (`@bot <link>` in any chat), optional, see below.
- **Languages:** English, Russian and Ukrainian, picked from each user's Telegram language.
- **Clear errors** (bot check/sign-in needed, age restriction, region lock, rate limit, too large, private...), and unexpected errors are forwarded to the admins.
- Runs from `nix run .` and ships a NixOS service module.

Ordinary web links and direct file links are ignored; only known media platforms are downloaded.

## Run Locally

```bash
BOT_TOKEN=123:token nix run .
```

With Python directly (needs `ffmpeg` on `PATH`):

```bash
python -m pip install .
BOT_TOKEN=123:token python -m tiktok_downloader_bot
```

## Configuration

Required:

```env
BOT_TOKEN=123:token
```

Optional settings (see `env.example` for the full list):

```env
AUTHORIZED_USER=123456789,987654321   # empty = everyone
ADMIN_USER=123456789                  # /stats and error notifications
BOT_WORK_DIR=/tmp/tiktok-downloader-bot
BOT_DOWNLOAD_DIR=/tmp/tiktok-downloader-bot/downloads
BOT_API_BASE=https://api.telegram.org
BOT_MAX_UPLOAD_MB=50
BOT_POLL_TIMEOUT=50
BOT_MAX_WORKERS=4                     # downloads running at the same time
BOT_USER_CONCURRENCY=2                # ...of which per user
BOT_MAX_LINKS=3                       # links handled per message
YTDLP_FORMAT=bv*+ba/best
YTDLP_NO_PLAYLIST=true
YTDLP_COOKIES=
YTDLP_COOKIES_FROM_BROWSER=
TELEGRAM_CACHE_CHAT_ID=               # only for inline mode
LOG_LEVEL=INFO
```

The work directory also holds `settings.json` (per-chat settings) and the `file_id` cache, so keep it on persistent storage.

Group note: to react to every normal group message, disable BotFather privacy mode for the bot. With privacy mode enabled, Telegram only sends commands, replies, mentions and service-visible messages to the bot. In groups, supported media links are handled for everyone even when `AUTHORIZED_USER` is set; the allowlist still protects commands, settings and private-chat use. Group admins can turn auto-download off in `/settings`; the bot then reacts only to `/dl <link>` and `@bot <link>`.

## Inline Mode

Enable inline mode for the bot in BotFather (`/setinline`) and turn on inline feedback (`/setinlinefeedback`, 100%). Set `TELEGRAM_CACHE_CHAT_ID` to a private channel or group where the bot is allowed to post. Typing `@bot <link>` then offers a "Download" result; once chosen, the bot uploads the file to the cache chat once, and swaps it into the message. Already cached links are offered as ready media immediately. Albums cannot be sent inline.

## NixOS Module

```nix
{
  inputs.tiktok-downloader-bot.url = "github:TimurSl/tiktok-downloader-bot";

  outputs = { self, nixpkgs, tiktok-downloader-bot, ... }: {
    nixosConfigurations.host = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        tiktok-downloader-bot.nixosModules.default
        {
          services.tiktok-downloader-bot = {
            enable = true;
            tokenFile = "/run/secrets/tiktok-downloader-bot-token";
            adminUsers = [ 123456789 ];
            # cacheChatId = "-1001234567890";   # inline mode only
          };
        }
      ];
    };
  };
}
```

The module rejects `/nix/store` work and download directories because the bot must write temporary media files at runtime.

### Upgrading from `telegram-automation-bot`

The project was renamed. The old option path `services.telegram-automation-bot.*` still works (it is forwarded with a deprecation warning), but the **defaults** moved: the service, system user and state directory are now `tiktok-downloader-bot` / `/var/lib/tiktok-downloader-bot`. To keep using the old user and directory, set `user`, `group`, `workDir` (and `downloadDir`) explicitly. The Python package is now `tiktok_downloader_bot`, and the executable `tiktok-downloader-bot`.

Guest mode was removed together with its `guest_message` handling. `cacheChatId` / `TELEGRAM_CACHE_CHAT_ID` is now only used by inline mode.

## YouTube Bot Checks

If YouTube returns `Sign in to confirm you're not a bot` (the bot then tells users that cookies are needed), export browser cookies to Netscape `cookies.txt` format and point yt-dlp at that file:

```nix
services.tiktok-downloader-bot.cookiesFile = "/var/lib/tiktok-downloader-bot/youtube-cookies.txt";
```

or in `.env`:

```env
YTDLP_COOKIES=/var/lib/tiktok-downloader-bot/youtube-cookies.txt
```

For a NixOS service, a cookie file is usually more reliable than `cookiesFromBrowser`, because the service user normally cannot read your desktop browser profile.

## Tests

```bash
nix develop -c python -m unittest discover -s tests
```
