# En vert som assistenten kan nå

Dette er en prøveordning utenfor løftet for 1.0. Lokal bruk på Mac over stdio
er fortsatt standard. Funksjonen er med fra 1.0.0rc3. En første ekte prøve på
Fly.io 17. november 2026 bekreftet utrulling, stopp og start ved kall, flytting
av innloggingene for Spond og Hoopit, lesing på verten og tilkobling fra
Claude på nett og Codex CLI. ChatGPT, Claude på mobil, planlagt skykjøring,
de andre kildene og drift over tid er ennå ikke prøvd. Se
[Prøvd på ekte vert](#prøvd-på-ekte-vert).

Verten kjører samme lesende MCP-server i en Linux-container. Den henter bare
når assistenten spør; den har ingen egen modell, kalenderkobling eller
planlegger. Du logger fortsatt inn i veiviseren på Macen, og flytter så
innloggingene til verten. Macen skal deretter ikke fornye dem.

## Kostnad og personvern

Fly.io gir container, vedvarende disk, HTTPS, hemmeligheter og start ved kall.
Oppsettet bruker én shared-cpu-1x-maskin med 256 MB og én disk på 1 GB. Den
stopper når den er ubrukt. Ingen vedlikeholdskall holder den våken. I prøven
stoppet Fly maskinen etter omtrent fem minutter uten kall. Neste kall startet
den igjen: maskinen var i gang etter omtrent 0,65 sekunder og svarte etter
omtrent 2,4 sekunder, og kallet lyktes uten feil i klienten.

Kontrollert 17. november 2026: [Fly-prisene](https://fly.io/pricing/) viser
2,19 USD per måned for kontinuerlig kjøring med 256 MB i billigste region,
0,15 USD per GB disk og måned og 0,15 USD per GB rotfilsystem for en stoppet
maskin. Stockholm kan koste mer. Nettverk, sikkerhetskopier, valutakurs og
avgifter kommer eventuelt i tillegg; 20–30 kr er et anslag, ingen prisgaranti.
Fly tilbyr startkreditt, men dette oppsettet forutsetter betalt bruk. Ikke
baser driften på en varig gratisplan. Se også
[prisdetaljene](https://fly.io/docs/about/pricing/).

Dette er en større risiko enn lokal bruk: en uoffisiell kobling med en
BankID-opprettet økt blir tilgjengelig via en offentlig adresse. Fly har
kontroll over verten og kan i prinsippet lese øktene. Innhold som assistenten
henter, kan sendes til AI-leverandøren. Eierkoden gir full lesetilgang til de
aktiverte kildene. Oppbevar den i passordbehandler, aldri i chat eller Git.
OAuth er et vern, ikke en garanti mot feil i kode, vert eller assistent.

## Fly.io, steg for steg

Kjør kommandoene under fra kildeutsjekkingen, med 1.0.0rc3 eller nyere.
Installer først etter [oppsettsveiledningen](kom-i-gang.md), og logg inn i den
lokale veiviseren. Google-klienten for Gmail følger samme flytting som økten.

1. Installer `flyctl`, for eksempel med `brew install flyctl`.
2. Opprett selv Fly-kontoen og godkjenn kostnaden. Kjør `flyctl auth login`.
3. Kjør på Macen:

   ```sh
   familiekilder-mcp deploy fly --app DITT-UNIKE-APPNAVN
   ```

Kommandoen lager app og disk, setter hemmeligheter via stdin og bygger fra
kildeutsjekkingen på Flys byggetjeneste. Den bruker nøyaktig én maskin, aldri en reservemaskin.
Utrulling skjer med `immediate`, som gir et kort avbrudd. En eksisterende app
med flere maskiner avvises før endringer. Installasjons-ID-en endres ikke ved
ny utrulling. `--region arn` og `--org personal` er standard.

Adressen er `https://DITT-UNIKE-APPNAVN.fly.dev/mcp`. Eierkoden leveres før
hashen sendes til Fly, og vises én gang på terminalen. Behold den også hvis
bygg eller utrulling feiler; et nytt forsøk kan bruke den samme koden. Når en kodeagent kjører uten terminal, skrives den i en privat
fil, og bare filstien vises. Åpne filen selv, lagre koden og slett filen.
Kodeagenten skal ikke lese eller lime inn koden i samtalen.

Du får tilbud om å flytte innloggingene. Velg senere med `--no-move`, eller
bestill flytting uttrykkelig med `--move`. Flytting kan også kjøres alene:

```sh
familiekilder-mcp remote move https://DITT-UNIKE-APPNAVN.fly.dev
```

Eierkoden leses skjult fra terminalen, eller fra én linje på stdin ved
agentkjøring. Den skal aldri stå i argumentene. `--source KILDE` begrenser
flyttingen og kan gjentas. Uten dette flyttes påslåtte kilder som har en økt.
Skriverettigheter følger ikke med; verten tilbyr bare lesing.

## Codex CLI

Codex CLI fra OpenAI er prøvd mot en ekte vert med versjon 0.169.0. Legg til
verten slik (`familiekilder` er bare navnet Codex bruker på koblingen):

```sh
codex mcp add familiekilder --url https://DITT-UNIKE-APPNAVN.fly.dev/mcp
```

Med `--url` registrerer Codex serveren og starter innloggingen selv i
nettleseren. Vertens godkjenningsside viser appen som «Codex». Skriv eierkoden
der, ikke i terminalen og ikke i en samtale med Codex. Vil du heller få
adressen skrevet ut enn å få nettleseren åpnet, bruk:

```sh
codex mcp login familiekilder --no-browser
```

Etter godkjenningen kan Codex bruke verktøyene, også i en kjøring med
`codex exec`.

## Claude og ChatGPT

Faktaene her er kontrollert mot originalkilder 17. november 2026. Menyer,
abonnementer og utrulling kan endres. Claude på nett er prøvd med denne
serveren; se [Prøvd på ekte vert](#prøvd-på-ekte-vert). ChatGPT er ennå ikke
prøvd; prøvene med de andre klientene erstatter ikke det.

**Claude:** [egne koblinger](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)
finnes også på Free, begrenset til én. Legg til under **Customize > Connectors
> + Add > Add custom connector** på nett. Bruk vertens `/mcp`-adresse,
OAuth-innlogging og automatisk klientregistrering der valget vises. Skriv
eierkoden bare på vertens godkjenningsside. Claude dokumenterer at eksterne
koblinger brukes gjennom skyen også fra mobil. Kontroller dette med egen
konto; denne koblingen er prøvd på nett, men ikke i mobilklienten.
[Autentisering](https://claude.com/docs/connectors/building/authentication)
støtter DCR, som denne serveren bruker. CIMD annonseres ikke.

**ChatGPT:** Åpne **Plugins > + > Add custom MCP server** på nett, oppgi
adressen, velg OAuth og gjennomgå risikovarselet. Utviklermodus skal ikke slås
på først i den nåværende Plugins-flyten. Etter godkjenning: installer og bruk
koblingen i en ny samtale. [MCP-veiledningen](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)
dokumenterer full MCP for Business og Enterprise/Edu og lesende MCP for Pro.
[Plugins-veiledningen](https://help.openai.com/en/articles/20001256-plugins-in-chatgpt)
og kontoreglene må kontrolleres før kjøp: en synlig katalog beviser ikke at
din plan kan legge til en egen MCP-server. Plus, Free og Go loves ikke her.
MCP-siden sier at tilkobling opprettes på nett, ikke mobil. Mobilbruk og
planlagt ChatGPT-kjøring med denne serveren er ikke bekreftet.

Begge klientene kan registrere OAuth-klienter automatisk. Registrering gir
ingen tilgang før eierkoden godkjennes. Serveren annonserer `offline_access`
og gir roterende refresh-token. Se [OpenAIs autentiseringskrav](https://developers.openai.com/plugins/build/auth).
Access-token varer én time; refresh-token varer inntil 90 dager fra siste
fornyelse. Dette forlenger ikke økten hos skolen eller barnehagen.

## En kveldsoppgave i skyen

[Claude Cowork](https://support.claude.com/en/articles/13854387-schedule-recurring-tasks-in-claude-cowork)
dokumenterer planlagte skyoppgaver med koblinger på betalte planer, også når
Macen sover. Free-kobling betyr altså ikke gratis planlagt kjøring. Velg en
skyoppgave uten lokal mappe eller «Require this computer». Koble til verten og
eventuelt kalenderen separat, og prøv først oppgaven manuelt:

> Hver kveld kl. 19: les nye beskjeder og aktiviteter gjennom Familiekilder.
> Oppsummer hva vi må gjøre i morgen, og ta med frister. Si tydelig fra om en
> kilde ikke kunne leses. Ikke regn access_expired alene som tapt tilgang.

Kalenderendringer krever en egen kalenderkobling og en uttrykkelig bestilling.
Kontroller én planlagt kjøring med Macen avslått før du stoler på rutinen.
Ingen slik klientprøve er utført for dette oppsettet. ChatGPTs muligheter må
prøves særskilt; det gis ikke et løfte om denne oppgaven på mobil eller i skyen.

## Prøvd på ekte vert

Den første ekte prøven ble gjort 17. november 2026 på Fly.io i region `arn`,
med én shared-cpu-1x-maskin med 256 MB, én disk på 1 GB og `flyctl` v0.5.11.
Utrullingen ble gjort fra kildeutsjekkingen med
`familiekilder-mcp deploy fly --no-move`.

Dette virket:

- **Utrulling.** App, disk og hemmeligheter ble opprettet. Flys byggetjeneste
  bygde bildet; `flyctl` oppga 69 MB. Appen fikk nøyaktig én maskin og HTTPS
  på `fly.dev`-adressen. Det aller første forsøket feilet på grunn av en feil i
  stien til Dockerfile. Feilen ble rettet samme dag. Tre nye utrullinger til
  samme app beholdt eierkoden, installasjons-ID-en, godkjente apper og
  flyttede innlogginger.
- **Adressene.** `/healthz` svarte 200. `/mcp` uten token svarte 401 med
  henvisning til ressursmetadataene, og begge metadataadressene svarte.
- **Stopp og start.** Fly stoppet maskinen etter omtrent fem minutter uten
  kall. Neste kall startet den: maskinen var i gang etter omtrent 0,65 sekunder
  og svarte etter omtrent 2,4 sekunder. MCP-kallet lyktes uten feil i klienten.
  Etter rettingen samme dag logger et stopp
  «Main child exited normally with code: 0» og ingen traceback.
- **Flytting.** `remote move ADRESSE --source spond` og deretter
  `--source hoopit`, med eierkoden på én linje på stdin. Begge ble meldt som
  flyttet. På Macen viste `connections` deretter statusen `moved_to_host` for
  begge, og de lokale øktfilene var borte.
- **Lesing på verten.** `spond_list_items` ga ekte oppføringer.
  Access-tokenet for Spond hadde vært utløpt siden 31. oktober, og verten
  fornyet det ved første lesing. Det første kallet tok derfor omtrent 20
  sekunder. `hoopit_list_items` og `spond_list_chats` virket også. Tokenet for
  Hoopit hadde vært utløpt siden 30. oktober og ble også fornyet på verten.
- **OAuth med ekte klienter.** MCP Python SDK-klienten gikk gjennom
  registrering, PKCE og godkjenningssiden med eierkode. Tokenene ble gjenbrukt
  i senere kjøringer, etter omstart av maskinen og etter ny utrulling.
  [Codex CLI](#codex-cli) 0.169.0 koblet til som beskrevet over. Deretter kalte
  en modellkjøring med `codex exec` `family_connections` og
  `spond_list_items` på verten. Codex-innloggingen i prøven ble fullført uten
  nettleser. Sendingen fra godkjenningssiden tilbake til assistenten i
  nettleseren ble derfor først prøvd med Claude på nett, der Brave stoppet den.
  Feilen ble rettet samme dag.
- **Claude på nett.** Etter rettingen ble koblingen lagt til i Claude i Brave
  med **Customize > Connectors > Add > Add custom connector**. Vertens
  godkjenningsside viste appen som «Claude». Etter eierkoden og «Godkjenn»
  gikk nettleseren tilbake til claude.ai, og koblingen sto som tilkoblet med
  20 leseverktøy. `family_connections` og `spond_list_items` ble deretter
  kalt gjennom Claude-koblingen fra Claude Code, som bruker koblingene
  kontoen har på nett. En samtale i nettgrensesnittet ble ikke fullført, fordi
  kontoens bruksgrense var nådd.
- **Fornyelse av OAuth-token.** Etter én time var klientens access-token
  utløpt. MCP SDK-klienten fornyet det med refresh-tokenet, og lesingen
  virket videre. Verten fornyet da Hoopit-økten en gang til.
- **Tilbakekalling.** `remote clients` og `remote revoke --client ID` virket
  gjennom `flyctl ssh console --app APP -C '...'` som beskrevet under
  [Feilsøking og tilbakekalling](#feilsøking-og-tilbakekalling).
- **Loggen.** Vertsloggen ble gjennomsøkt. Den inneholdt ingen eierkode,
  access- eller refresh-token, bearer-header eller OAuth-parametere fra
  adresser.

Dette er ennå ikke prøvd:

- Tilkobling fra ChatGPT på nett og fra Claude på mobil, og en hel samtale i
  Claudes nettgrensesnitt.
- En planlagt skyoppgave som kjører mens Macen er av.
- Kilder som logger inn med BankID (Vigilo, Skolemelding, IST Home og Flyt),
  og Gmail, på verten. Bare Spond og Hoopit ble flyttet.
- Fornyelse over dager og uker, og hva verten faktisk koster per måned.
- Word-vedlegg på verten (byggvalget `WITH_LIBREOFFICE=1`).

## Når en innlogging må fornyes

Logg inn på nytt i veiviseren på Macen, og kjør `remote move ADRESSE` igjen.
Det erstatter de flyttede øktene på verten. Et lokalt flyttemerke fjernes først
ved vellykket ny innlogging. `family_connections` og veiviseren viser at den
forrige økten er flyttet, også etter avbrudd før lokal sletting.

Flyttingen bruker en kortlivet engangsnøkkel fra verten og X25519,
HKDF-SHA256 og AES-256-GCM. Pakkeformat, versjon, vert og nøkkel-ID inngår i
ekthetskontrollen. Transporten autentiseres med HTTPS og eierkoden. En kopiert
pakke virker ikke hos en annen vert, og nøkkelen kan bare brukes én gang.
Klientoppsett, økt og tokenjournal følger med; lesebufferen gjør det ikke.

Under kildelåsene sperres lokal fornyelse før pakken sendes. Ved et klart
avslag før import beholdes innloggingene lokalt. Ved tapt svar eller avbrudd
beholdes sperren. **Verste utfall er ny innlogging på Macen.** Ikke fjern
flyttemerker eller gjenopprett gamle økter fra backup. Det kan skape to
installasjoner som fornyer samme tokenrekke.

Verten lagrer eierbindingen atomisk før de importerte øktfilene skrives.
En vertsøkts manglende eierbinding gir avslag, ikke automatisk overtakelse.
Installasjons-ID-en ligger som hemmelighet utenfor datadisken. Ta vare på den
separat dersom du tar backup; uten den skal en kopiert disk ikke kunne fornye.
Gi aldri samme disk og ID til to kjørende verter. Dette er ett eieroppsett,
ikke en tjeneste for flere familier.

## Egen Docker-vert eller hjemmeboks

Har du alt en hjemmeboks, kan den kjøre bildet bak en HTTPS-tunnel uten egen
vertsleie. Strøm, vedlikehold og eventuell tunnelpris kommer i tillegg. Ingen
egen veiviser settes opp. Fra kildeutsjekkingens rot:

```sh
docker build -f src/familiekilder/data/container/Dockerfile -t familiekilder-mcp .
familiekilder-mcp remote init --public-url https://DIN-ADRESSE --env-file /SIKKER/STI/vert.env
docker volume create familiekilder-data
docker run -d --name familiekilder --env-file /SIKKER/STI/vert.env \
  -p 127.0.0.1:8080:8080 -v familiekilder-data:/data familiekilder-mcp
```

La tunnelen videresende HTTPS til `127.0.0.1:8080`, bevare offentlig Host og
ikke logge forespørselsinnhold, OAuth-parametere eller hemmelige headere.
Kjør bare én container. Ikke legg miljøfilen på datadisken eller i Git.
`/healthz` viser bare `ok`. HTTP krever offentlig adresse, scrypt-hash av
koden og, på Linux, installasjons-ID. Ingen usikret HTTP-modus finnes.

Miljøvariabler: `FAMILIEKILDER_PUBLIC_URL`, `FAMILIEKILDER_OWNER_CODE_HASH`,
`FAMILIEKILDER_INSTALLATION_ID` og `FAMILIEKILDER_ROOT`. Ekstra returverter kan
angis med `FAMILIEKILDER_REDIRECT_HOSTS` (kommaseparert); ikke legg til en vert
du ikke stoler på. Standard er Claude, ChatGPT og loopback. Returadressen må
fortsatt være registrert og stemme nøyaktig.

Bildet bruker Python 3.12 og Poppler. Serverprosessen kjører som UID 10001;
oppstarten klargjør disken og slipper root før serveren startes. Dataroten har
modus 0700. LibreOffice er et valgfritt byggargument `WITH_LIBREOFFICE=1`.
Standardbildet prioriterer den lille verten; Word-vedlegg krever tilvalget.
Målt med Docker på arm64 17. november 2026: standardbildet var 246 536 964
byte (omtrent 247 MB), bildet med LibreOffice 570 461 728 byte (omtrent
571 MB). Begge brukte omtrent 49–51 MiB i ro etter OAuth og omstart, under en
grense på 256 MiB. LibreOffice mer enn dobler bildestørrelsen og er derfor
ikke med som standard. Minne ved Word-konvertering er ikke målt; 256 MB er
ikke kvalifisert for det. Containerprøven bekreftet full SDK-OAuth,
verktøyliste, gjenbruk av token etter omstart, UID 10001 og datamodus 0700.

## Feilsøking og tilbakekalling

- Fly stopper en ubrukt maskin med SIGINT. Serveren avslutter da rolig med
  status 0, også ved SIGTERM. Fly-loggen viser da
  «Main child exited normally with code: 0». En traceback i loggen er altså en
  ekte feil.
- Mangler `flyctl` eller innlogging? Kommandoen sier hva som må installeres
  eller at `flyctl auth login` må kjøres. Eierkoden er noe annet enn Fly-login.
- Feil kode fem ganger på 15 minutter eller 20 ganger på ett døgn sperrer nye
  forsøk midlertidig. Omstart nullstiller ikke telleren.
- Svært store klientregistreringer avvises. OAuth-lageret har en samlet
  størrelsesgrense som kontrolleres før eksisterende tilstand erstattes.
- Får du 401 uten token? Det er normalt og skal starte OAuth. Kontroller
  `/.well-known/oauth-protected-resource/mcp` og
  `/.well-known/oauth-authorization-server` uten å dele innhold med tokens.
- En app beholder en gammel verktøyliste? Flytt innloggingene før du kobler
  assistenten til. Etter endringer må ChatGPT-administrator eventuelt
  oppdatere godkjente verktøy. Serveren oppdaterer sin egen katalog ved kall.
- Vil du trekke tilbake en app? På egen Docker-vert:

  ```sh
  docker exec --user 10001 familiekilder familiekilder-mcp remote clients
  docker exec --user 10001 familiekilder familiekilder-mcp remote revoke --all
  ```

  På Fly, kjør samme kommando i eksisterende maskin, ikke med `fly console`
  som oppretter en ny: `flyctl ssh console --app APP -C 'python -m familiekilder.container remote clients'`
  og `flyctl ssh console --app APP -C 'python -m familiekilder.container remote revoke --all'`.
  `--client ID` trekker bare tilbake én klient. Endringen gjelder neste kall.
  Ny eierkode lages med `deploy fly --app APP --new-owner-code`; tilbakekall
  også eksisterende tokens ved mistanke om misbruk. Ikke mist koden før ny
  utrulling er fullført.
