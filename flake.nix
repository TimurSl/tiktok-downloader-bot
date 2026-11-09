{
  description = "Telegram bot that downloads TikTok, YouTube, Instagram and other media";

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
          tiktok-downloader-bot = python.pkgs.buildPythonApplication {
            pname = "tiktok-downloader-bot";
            version = "2.0.0";
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
              wrapProgram $out/bin/tiktok-downloader-bot \
                --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.ffmpeg ]}
            '';

            pythonImportsCheck = [ "tiktok_downloader_bot" ];

            meta.mainProgram = "tiktok-downloader-bot";
          };

          default = self.packages.${system}.tiktok-downloader-bot;
        });

      apps = forAllSystems (system: {
        tiktok-downloader-bot = {
          type = "app";
          program = "${self.packages.${system}.tiktok-downloader-bot}/bin/tiktok-downloader-bot";
        };
        default = self.apps.${system}.tiktok-downloader-bot;
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

      nixosModules.default = import ./nix/modules/tiktok-downloader-bot.nix { inherit self; };
      nixosModules.tiktok-downloader-bot = self.nixosModules.default;
    };
}
