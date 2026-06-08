{
  description = "Telegram media downloader bot with guest mode";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      pkgsFor = system: import nixpkgs { inherit system; };
    in
    {
      packages = forAllSystems (system:
        let
          pkgs = pkgsFor system;
          python = pkgs.python3;
        in
        {
          telegram-automation-bot = python.pkgs.buildPythonApplication {
            pname = "telegram-automation-bot";
            version = "1.0.0";
            pyproject = true;

            src = pkgs.lib.cleanSource ./.;

            build-system = [ python.pkgs.setuptools ];
            dependencies = with python.pkgs; [
              python-dotenv
              requests
              yt-dlp
            ];

            nativeBuildInputs = [ pkgs.makeWrapper ];

            postInstall = ''
              wrapProgram $out/bin/telegram-automation-bot \
                --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.ffmpeg ]}
            '';

            pythonImportsCheck = [ "telegram_automation_bot" ];
          };

          default = self.packages.${system}.telegram-automation-bot;
        });

      apps = forAllSystems (system: {
        telegram-automation-bot = {
          type = "app";
          program = "${self.packages.${system}.telegram-automation-bot}/bin/telegram-automation-bot";
        };
        default = self.apps.${system}.telegram-automation-bot;
      });

      devShells = forAllSystems (system:
        let
          pkgs = pkgsFor system;
          python = pkgs.python3.withPackages (ps: with ps; [
            python-dotenv
            requests
            yt-dlp
          ]);
        in
        {
          default = pkgs.mkShell {
            packages = [
              python
              pkgs.ffmpeg
              pkgs.yt-dlp
            ];
          };
        });

      formatter = forAllSystems (system: (pkgsFor system).nixpkgs-fmt);

      nixosModules.default = import ./nix/modules/telegram-automation-bot.nix { inherit self; };
      nixosModules.telegram-automation-bot = self.nixosModules.default;
    };
}
