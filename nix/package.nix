{ lib, python3Packages, balena-cli, openssh }:

let
  project = builtins.fromTOML (builtins.readFile ../pyproject.toml);
in
python3Packages.buildPythonApplication {
  pname = project.project.name;
  version = project.project.version;
  pyproject = true;
  src = lib.cleanSource ../.;

  build-system = [ python3Packages.setuptools ];
  dependencies = with python3Packages; [ rich pyte ];

  # Keep the system's privileged sudo; bundle the unprivileged runtime tools.
  makeWrapperArgs = [ "--prefix PATH : ${lib.makeBinPath [ balena-cli openssh ]}" ];

  doCheck = true;
  checkPhase = ''
    runHook preCheck
    python -m unittest discover -s tests -v
    runHook postCheck
  '';
  pythonImportsCheck = [ "attraccess_tunnel.cli" ];

  meta = {
    description = project.project.description;
    homepage = project.project.urls.Homepage;
    mainProgram = "balena-proxy-tui";
    platforms = lib.platforms.linux ++ lib.platforms.darwin;
  };
}
