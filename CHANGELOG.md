# Endringer

## Ikke utgitt

- README-en forklarer nå de to måtene å kjøre serveren på, på Macen eller på
  din egen konto hos Fly.io, med en tabell over forskjellene og oppsett for
  begge. Prisavsnittet i veiledningen er kontrollert på nytt.
- `familiekilder-mcp deploy fly --word` bygger verten med LibreOffice, så den
  kan lese Word-vedlegg, og gir maskinen 512 MB vekselminne (swap). Uten det
  drepte Linux LibreOffice på en ekte 256 MB-maskin. Bildet blir omtrent 2,5
  ganger så stort. Valget
  huskes ikke; ruller du ut uten `--word`, forsvinner Word-støtten. Kommandoen
  sier derfor ved hver utrulling om Word-vedlegg er på eller av. Word-lesing
  er prøvd på ekte vert med et oppdiktet dokument: det virker, men tar 20–30
  sekunder. Se
  [Word-vedlegg på verten](docs/alltid-pa.md#word-vedlegg-på-verten).
- LibreOffice legger nå sine midlertidige filer i vedleggets private
  arbeidsmappe, ikke i systemets felles mappe for midlertidige filer.
- Veiledningen sier også at Claude og ChatGPT på iPhone og en planlagt
  skykjøring fra Claude Code er prøvd mot en ekte vert (18. november 2026).
- Veiledningen for [alltid-på-oppsettet](docs/alltid-pa.md) sier nå at
  ChatGPT på nett er prøvd mot en ekte vert (Pro-konto, 18. november 2026),
  og at kommandoene for tilbakekalling på Fly trenger en maskin som er i
  gang. Ingen kode er endret.

## 1.0.0rc3

- Prøveordning for egen Linux-vert, utenfor løftet for 1.0: lesende MCP over
  streamable HTTP med OAuth, eierkode, roterende refresh-token, kryptert
  flytting av innlogginger fra Mac og veiledet Fly.io-oppsett med
  `familiekilder-mcp deploy fly`. Lokal stdio er fortsatt standard. Prøvd
  17. november 2026 på en ekte Fly-vert: utrulling, stopp og start ved kall,
  flyttede innlogginger for Spond og Hoopit, lesing på verten og tilkobling fra
  Claude på nett og Codex CLI. ChatGPT på nett og Claude på mobil er ennå ikke
  prøvd, og heller ikke de andre kildene på verten. Se
  [alltid-på-oppsettet](docs/alltid-pa.md).
- Tre feil som den ekte prøven avdekket, er rettet. `deploy fly` fant ikke
  Dockerfile med `flyctl` v0.5.11, så bygget feilet. Når Fly stopper en ubrukt
  maskin, avslutter serveren nå rolig med status 0 uten traceback i loggen,
  også ved SIGTERM. Godkjenningssiden stoppet Brave og Chrome fra å sende
  svaret tilbake til assistenten etter «Godkjenn» eller «Avvis», så ingen
  tilkobling gjennom nettleseren kunne fullføres. Nå kan siden sende svaret til
  akkurat den adressen assistenten oppga, og ingen andre steder. Sender du
  skjemaet en gang til etter svaret, står det nå at forespørselen allerede er
  besvart, ikke at den er utløpt.
- Veiviseren logger inn med ett klikk. Vigilo, Skolemelding, Spond, Hoopit og
  IST Home har innebygd appidentitet fra leverandørens offisielle app, så
  «Logg inn» trenger ingen oppsettsfil. Flyt spør om kommune i
  innloggingsdialogen, og Gmail trenger fortsatt din egen Google-fil.
  «Legg til oppsett» og «Oppsett og innlogging» er fjernet fra veiviseren;
  andre oppsettsfiler importeres med kommandolinjen. Se
  [klientoppsett](docs/klientoppsett.md).
- I veiviseren kan du velge en annen kommune for Flyt og en annen Google-fil
  for Gmail før du logger inn. Med en lagret Flyt-innlogging bytter du kommune
  med «Logg inn på nytt»; da logges du ut av Flyt først.
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
- Pakkesjekken på GitHub kjører nå på Linux med Python 3.11 og 3.14.

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
