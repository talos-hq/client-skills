{ lib, stdenvNoCC, python3, src }:

stdenvNoCC.mkDerivation {
  pname = "hermes-consent-exec";
  version = "0.1.0";

  inherit src;
  dontUnpack = true;
  dontBuild = true;

  installPhase = ''
    runHook preInstall
    install -Dm0755 ${src} $out/bin/hermes-consent-exec
    substituteInPlace $out/bin/hermes-consent-exec \
      --replace-fail '#!/usr/bin/env python3' '#!${python3}/bin/python3'
    runHook postInstall
  '';

  meta = {
    description = "Consent-gated root helper for Hermes agents";
    license = lib.licenses.mit;
    mainProgram = "hermes-consent-exec";
    platforms = lib.platforms.linux;
  };
}
