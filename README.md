# Telegram Automation Bot

Production Telegram bot for media downloads.

## Features

- Downloads media links with `yt-dlp`, uploads the result to Telegram, then deletes temporary files.
- Supports TikTok videos, photo posts, and TikTok short/redirect links. Photo posts are sent as Telegram albums, splitting every 10 images into a new album message.
- Supports YouTube quality selection with retry, reselect-quality, and cancel buttons.
- Reacts to media links in groups even when `AUTHORIZED_USER` is set; the allowlist still protects commands and private-chat use.
- Supports Telegram Bot API guest updates through `guest_message` and `answerGuestQuery`.
- Runs from `nix run .` and includes a NixOS service module.

## Run Locally

```bash
BOT_TOKEN=123:token nix run .
```

With Python directly:

```bash
python -m pip install .
BOT_TOKEN=123:token python -m telegram_automation_bot
```

## Configuration

Required:

```env
BOT_TOKEN=123:token
```

Common optional settings:

```env
AUTHORIZED_USER=123456789,987654321
BOT_WORK_DIR=/tmp/telegram-automation-bot
BOT_DOWNLOAD_DIR=/tmp/telegram-automation-bot/downloads
BOT_API_BASE=https://api.telegram.org
BOT_MAX_UPLOAD_MB=50
BOT_POLL_TIMEOUT=50
YTDLP_FORMAT=bv*+ba/best
YTDLP_NO_PLAYLIST=true
YTDLP_COOKIES=
YTDLP_COOKIES_FROM_BROWSER=
TELEGRAM_CACHE_CHAT_ID=
LOG_LEVEL=INFO
```

Group note: to react to every normal group message, disable BotFather privacy mode for the bot. With privacy mode enabled, Telegram only sends commands, replies, mentions, and service-visible messages to the bot.

Guest mode note: Telegram guest replies use `answerGuestQuery`, which sends an inline result. Local files cannot be uploaded directly in that call, so set `TELEGRAM_CACHE_CHAT_ID` to a private chat/channel where the bot can upload downloaded files and reuse the returned `file_id`. Telegram guest replies cannot send albums; TikTok photo albums must be sent in a normal chat with the bot.

## NixOS Module

```nix
{
  inputs.telegram-automation-bot.url = "path:/home/melvi/Work/telegram-automation-bot-framework";

  outputs = { self, nixpkgs, telegram-automation-bot, ... }: {
    nixosConfigurations.host = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        telegram-automation-bot.nixosModules.default
        {
          services.telegram-automation-bot = {
            enable = true;
            tokenFile = "/run/secrets/telegram-automation-bot-token";
            workDir = "/var/lib/telegram-automation-bot";
            downloadDir = "/var/lib/telegram-automation-bot/downloads";
            cacheChatId = "-1001234567890";
          };
        }
      ];
    };
  };
}
```

The module rejects `/nix/store` work and download directories because the bot must write temporary media files at runtime.

## YouTube Bot Checks

If YouTube returns `Sign in to confirm you're not a bot`, export browser cookies to Netscape `cookies.txt` format and point yt-dlp at that file:

```nix
services.telegram-automation-bot.cookiesFile = "/var/lib/telegram-automation-bot/youtube-cookies.txt";
```

or in `.env`:

```env
YTDLP_COOKIES=/var/lib/telegram-automation-bot/youtube-cookies.txt
```

For a NixOS service, a cookie file is usually more reliable than `cookiesFromBrowser`, because the service user normally cannot read your desktop browser profile.

## Tests

```bash
nix develop -c python -m unittest discover -s tests
```
