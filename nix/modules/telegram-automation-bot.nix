{ self }:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.telegram-automation-bot;
  inherit (lib) mkEnableOption mkIf mkOption optional types;
  system = pkgs.stdenv.hostPlatform.system;
  envList =
    [
      "BOT_WORK_DIR=${cfg.workDir}"
      "BOT_DOWNLOAD_DIR=${cfg.downloadDir}"
      "BOT_DATABASE=${cfg.databasePath}"
      "BOT_API_BASE=${cfg.botApiBase}"
      "BOT_MAX_UPLOAD_MB=${toString cfg.maxUploadMB}"
      "BOT_POLL_TIMEOUT=${toString cfg.pollTimeout}"
      "YTDLP_FORMAT=${cfg.ytdlpFormat}"
      "YTDLP_NO_PLAYLIST=${if cfg.ytdlpNoPlaylist then "true" else "false"}"
      "AUTHORIZED_USER=${lib.concatMapStringsSep "," toString cfg.authorizedUsers}"
    ]
    ++ optional (cfg.tokenFile != null) "BOT_TOKEN_FILE=${cfg.tokenFile}"
    ++ optional (cfg.token != null) "BOT_TOKEN=${cfg.token}"
    ++ optional (cfg.cacheChatId != null) "TELEGRAM_CACHE_CHAT_ID=${cfg.cacheChatId}"
    ++ optional (cfg.cookiesFile != null) "YTDLP_COOKIES=${cfg.cookiesFile}"
    ++ optional (cfg.cookiesFromBrowser != null) "YTDLP_COOKIES_FROM_BROWSER=${cfg.cookiesFromBrowser}"
    ++ lib.mapAttrsToList (name: value: "${name}=${value}") cfg.extraEnvironment;
in
{
  options.services.telegram-automation-bot = {
    enable = mkEnableOption "Telegram automation bot";

    package = mkOption {
      type = types.package;
      default = self.packages.${system}.default;
      defaultText = "self.packages.\${system}.default";
      description = "Package that provides the telegram-automation-bot executable.";
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
      default = "/var/lib/telegram-automation-bot";
      description = "Mutable bot state directory. This must not point into /nix/store.";
    };

    downloadDir = mkOption {
      type = types.str;
      default = "${cfg.workDir}/downloads";
      description = "Mutable temporary download directory used for yt-dlp output.";
    };

    databasePath = mkOption {
      type = types.str;
      default = "${cfg.workDir}/bot.sqlite3";
      description = "SQLite database path for RP commands.";
    };

    cacheChatId = mkOption {
      type = types.nullOr types.str;
      default = null;
      description = "Telegram chat ID used to cache guest-mode downloads and obtain file_ids.";
    };

    authorizedUsers = mkOption {
      type = types.listOf types.int;
      default = [ ];
      description = "Optional Telegram user IDs allowed to use normal bot commands. Empty means public.";
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
      default = "telegram-automation-bot";
      description = "System user that runs the bot.";
    };

    group = mkOption {
      type = types.str;
      default = "telegram-automation-bot";
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
        message = "services.telegram-automation-bot.workDir must be mutable and cannot be in /nix/store.";
      }
      {
        assertion = !(lib.hasPrefix "/nix/store/" cfg.downloadDir);
        message = "services.telegram-automation-bot.downloadDir must be mutable and cannot be in /nix/store.";
      }
      {
        assertion = cfg.token != null || cfg.tokenFile != null || cfg.environmentFile != null;
        message = "Set services.telegram-automation-bot.tokenFile, token, or environmentFile with BOT_TOKEN.";
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

    systemd.services.telegram-automation-bot = {
      description = "Telegram automation bot";
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
        ExecStart = "${cfg.package}/bin/telegram-automation-bot";
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
