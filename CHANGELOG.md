# Endringer

## Ikke utgitt

- Prøveordning for egen Linux-vert: lesende MCP over streamable HTTP med
  OAuth, eierkode, roterende refresh-token, kryptert flytting av innlogginger
  fra Mac og veiledet Fly.io-oppsett. Lokal stdio er fortsatt standard.
  Se [alltid-på-oppsettet](docs/alltid-pa.md); ekte vert og klienter gjenstår.

- Et klientoppsett som veiviseren har lagt inn fra pakkens innebygde oppsett,
  byttes nå ut av seg selv når en nyere pakke har et nyere. Det skjer når
  veiviseren åpnes og når MCP-serveren starter. Oppsett du har importert eller
  endret selv, byttes aldri, og innloggingen slettes ikke. Et oppsett
  veiviseren la inn i 1.0.0rc1 eller 1.0.0rc2 (i praksis Spond), har ikke merke
  og behandles som ditt eget. Se
  [klientoppsett](docs/klientoppsett.md#nytt-innebygd-oppsett-etter-en-pakkeoppdatering).
- Flyt velger bare en kommune når navnet er likt navnet i Flyts kommuneliste
  («kommune» på slutten kan sløyfes) eller organisasjonsnummeret stemmer. Før
  kunne en del av et navn velge en annen kommune med et lignende navn. Nå får du
  i stedet forslag med organisasjonsnummer, og ingenting lagres.
- Forespørsler til tjenestene oppgir ikke lenger pakkens navn i User-Agent.
  Alle kilder sender samme User-Agent som Brave eller Chrome på maskinen, med
  Chromium-versjonen fra den installerte nettleseren.

## 1.0.0rc2

- Ny kilde: Flyt (Flyt Foresatt fra Visma) for barnehage. Leser beskjeder,
  dagbok, meldinger, kalender, fravær og inn- og utsjekk, med vedlegg og bilder.
  Bare lesing; ingenting merkes som lest. Velg kommune med en oppsettsfil som
  `{"municipality": "Kommunenavn"}`, og logg inn med `auth flyt browser-login`.
  Prøvd mot en ekte barnehagekonto. Skole og SFO kommer senere.
- Innlogging gjennom Android over ADB (`android-login` og `android-device`) er
  fjernet. Logg inn i Brave med `browser-login` eller `browser-bootstrap`.
  Pakken trenger ikke lenger `websocket-client`.
- Veiledningen for nye tjenester forklarer hvordan du kartlegger tjenesten,
  begrenser kallene til lesing, holder listen og `get_item` like og tester mot
  egen konto.

## 1.0.0rc1

Første lanseringskandidat for familiekilder-mcp.

- Lokal MCP-server og kommandolinje for å lese beskjeder, aktiviteter og vedlegg.
- Egen innlogging, private profiler og mellomlagring på maskinen din.
- `familiekilder-mcp --version` viser pakkens versjon uten å åpne profilen.
- MCP-serveren oppgir samme versjon som pakken.
- Kort norsk introduksjon og egen oppsettsveiledning.
- Lanseringssjekk som bygger pakken, installerer den separat og kjører testene
  mot den installerte pakken, inkludert en faktisk MCP-tilkobling.
- Lokal webveiviser for oppsettsfiler, innlogging og tilkobling til Claude
  Desktop og lokal ChatGPT / Codex. Start med `scripts/setup`.
- Spond klargjøres uten en oppsettsfil; IST Home kan hente oppsettet fra egen Mac-app.
- Den selvstendige serveren tilbyr bare lesing, også med eldre skriverettigheter.
- Den selvstendige CLI-en bruker egen datarot, `familiekilder-mcp`. Eldre profiler
  kan fortsatt brukes med en eksplisitt `--root`.

Før stabil 1.0 gjenstår oppsett og praktisk kontroll som beskrevet i
[lanseringsplanen](docs/release.md).
