{ self }:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.tiktok-downloader-bot;
  inherit (lib) mkEnableOption mkIf mkOption optional types;
  system = pkgs.stdenv.hostPlatform.system;
  envList =
    [
      "BOT_WORK_DIR=${cfg.workDir}"
      "BOT_DOWNLOAD_DIR=${cfg.downloadDir}"
      "BOT_API_BASE=${cfg.botApiBase}"
      "BOT_MAX_UPLOAD_MB=${toString cfg.maxUploadMB}"
      "BOT_POLL_TIMEOUT=${toString cfg.pollTimeout}"
      "YTDLP_FORMAT=${cfg.ytdlpFormat}"
      "YTDLP_NO_PLAYLIST=${if cfg.ytdlpNoPlaylist then "true" else "false"}"
      "AUTHORIZED_USER=${lib.concatMapStringsSep "," toString cfg.authorizedUsers}"
      "ADMIN_USER=${lib.concatMapStringsSep "," toString cfg.adminUsers}"
      "BOT_MAX_WORKERS=${toString cfg.maxWorkers}"
      "BOT_USER_CONCURRENCY=${toString cfg.userConcurrency}"
      "BOT_MAX_LINKS=${toString cfg.maxLinksPerMessage}"
    ]
    ++ optional (cfg.tokenFile != null) "BOT_TOKEN_FILE=${cfg.tokenFile}"
    ++ optional (cfg.token != null) "BOT_TOKEN=${cfg.token}"
    ++ optional (cfg.cacheChatId != null) "TELEGRAM_CACHE_CHAT_ID=${cfg.cacheChatId}"
    ++ optional (cfg.cookiesFile != null) "YTDLP_COOKIES=${cfg.cookiesFile}"
    ++ optional (cfg.cookiesFromBrowser != null) "YTDLP_COOKIES_FROM_BROWSER=${cfg.cookiesFromBrowser}"
    ++ lib.mapAttrsToList (name: value: "${name}=${value}") cfg.extraEnvironment;
in
{
  imports = [
    # The package used to be called telegram-automation-bot; keep old configurations working.
    (lib.mkRenamedOptionModule
      [ "services" "telegram-automation-bot" ]
      [ "services" "tiktok-downloader-bot" ])
  ];

  options.services.tiktok-downloader-bot = {
    enable = mkEnableOption "TikTok downloader Telegram bot";

    package = mkOption {
      type = types.package;
      default = self.packages.${system}.default;
      defaultText = "self.packages.\${system}.default";
      description = "Package that provides the tiktok-downloader-bot executable.";
    };

    tokenFile = mkOption {
      type = types.nullOr types.str;
      default = null;
      description = "Path to a file containing BOT_TOKEN. Prefer this over token.";
    };

    token = mkOption {
      type = types.nullOr types.str;
      default = null;
      description = "Inline bot token. This stores the secret in the Nix store; prefer tokenFile.";
    };

    environmentFile = mkOption {
      type = types.nullOr types.str;
      default = null;
      description = "Optional systemd EnvironmentFile. Can provide BOT_TOKEN and other overrides.";
    };

    workDir = mkOption {
      type = types.str;
      default = "/var/lib/tiktok-downloader-bot";
      description = "Mutable bot state directory. This must not point into /nix/store.";
    };

    downloadDir = mkOption {
      type = types.str;
      default = "${cfg.workDir}/downloads";
      description = "Mutable temporary download directory used for yt-dlp output.";
    };

    cacheChatId = mkOption {
      type = types.nullOr types.str;
      default = null;
      description = "Optional private chat/channel the bot can post to. Needed only for inline mode: the bot uploads files there once to obtain reusable Telegram file_ids.";
    };

    authorizedUsers = mkOption {
      type = types.listOf types.int;
      default = [ ];
      description = "Optional Telegram user IDs allowed to use normal bot commands. Empty means public.";
    };

    adminUsers = mkOption {
      type = types.listOf types.int;
      default = [ ];
      description = "Telegram user IDs that may use /stats and receive notifications about unexpected errors.";
    };

    maxWorkers = mkOption {
      type = types.ints.positive;
      default = 4;
      description = "How many downloads may run at the same time.";
    };

    userConcurrency = mkOption {
      type = types.ints.positive;
      default = 2;
      description = "How many of those downloads one user may have running at once; the rest wait in a queue.";
    };

    maxLinksPerMessage = mkOption {
      type = types.ints.positive;
      default = 3;
      description = "How many links of a single message are downloaded.";
    };

    botApiBase = mkOption {
      type = types.str;
      default = "https://api.telegram.org";
      description = "Bot API base URL. Change this when using a local Bot API server.";
    };

    maxUploadMB = mkOption {
      type = types.int;
      default = 50;
      description = "Maximum file size to upload through the public Bot API.";
    };

    pollTimeout = mkOption {
      type = types.int;
      default = 50;
      description = "Long-poll timeout in seconds.";
    };

    ytdlpFormat = mkOption {
      type = types.str;
      default = "bv*+ba/best";
      description = "yt-dlp format selector.";
    };

    ytdlpNoPlaylist = mkOption {
      type = types.bool;
      default = true;
      description = "Pass noplaylist to yt-dlp to avoid accidental large playlist downloads.";
    };

    cookiesFile = mkOption {
      type = types.nullOr types.str;
      default = null;
      description = "Path to a Netscape cookies.txt file for yt-dlp, useful for YouTube bot checks.";
    };

    cookiesFromBrowser = mkOption {
      type = types.nullOr types.str;
      default = null;
      description = "Browser spec passed to yt-dlp cookies-from-browser, for example firefox or chromium:Profile 1. Usually less suitable for NixOS services than cookiesFile.";
    };

    user = mkOption {
      type = types.str;
      default = "tiktok-downloader-bot";
      description = "System user that runs the bot.";
    };

    group = mkOption {
      type = types.str;
      default = "tiktok-downloader-bot";
      description = "System group that runs the bot.";
    };

    extraEnvironment = mkOption {
      type = types.attrsOf types.str;
      default = { };
      description = "Extra environment variables for the service.";
    };
  };

  config = mkIf cfg.enable {
    assertions = [
      {
        assertion = !(lib.hasPrefix "/nix/store/" cfg.workDir);
        message = "services.tiktok-downloader-bot.workDir must be mutable and cannot be in /nix/store.";
      }
      {
        assertion = !(lib.hasPrefix "/nix/store/" cfg.downloadDir);
        message = "services.tiktok-downloader-bot.downloadDir must be mutable and cannot be in /nix/store.";
      }
      {
        assertion = cfg.token != null || cfg.tokenFile != null || cfg.environmentFile != null;
        message = "Set services.tiktok-downloader-bot.tokenFile, token, or environmentFile with BOT_TOKEN.";
      }
    ];

    users.groups.${cfg.group} = { };
    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.group;
      home = cfg.workDir;
    };

    systemd.tmpfiles.rules = [
      "d ${cfg.workDir} 0750 ${cfg.user} ${cfg.group} - -"
      "d ${cfg.downloadDir} 0750 ${cfg.user} ${cfg.group} - -"
    ];

    systemd.services.tiktok-downloader-bot = {
      description = "TikTok downloader Telegram bot";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];

      environment = lib.listToAttrs (map
        (entry:
          let
            parts = lib.splitString "=" entry;
            name = lib.head parts;
            value = lib.concatStringsSep "=" (lib.tail parts);
          in
          { inherit name value; })
        envList);

      serviceConfig = {
        ExecStart = "${cfg.package}/bin/tiktok-downloader-bot";
        User = cfg.user;
        Group = cfg.group;
        WorkingDirectory = cfg.workDir;
        Restart = "on-failure";
        RestartSec = "5s";
        EnvironmentFile = optional (cfg.environmentFile != null) cfg.environmentFile;
        ReadWritePaths = [ cfg.workDir cfg.downloadDir ];
        PrivateTmp = true;
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
      };
    };
  };
}
