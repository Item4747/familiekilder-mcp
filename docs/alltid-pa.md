# En vert som assistenten kan nå

Dette er en prøveordning utenfor løftet for 1.0. Lokal bruk på datamaskinen
over stdio er fortsatt standard. Funksjonen er med fra 1.0.0rc3. En første ekte prøve på
Fly.io 17. og 18. november 2026 bekreftet utrulling, stopp og start ved kall,
flytting av innloggingene for Spond og Hoopit, lesing på verten og tilkobling
fra Claude og ChatGPT på nett og mobil og fra Codex CLI, en planlagt
skykjøring og Word-vedlegg. Kildene med BankID-innlogging, Gmail med gyldig
innlogging og drift over tid er ennå ikke prøvd på ekte vert. Se
[Prøvd på ekte vert](#prøvd-på-ekte-vert).

Verten kjører samme lesende MCP-server i en Linux-container. Den henter bare
når assistenten spør; den har ingen egen modell, kalenderkobling eller
planlegger. Du logger fortsatt inn i veiviseren på datamaskinen din, og
flytter så innloggingene til verten. Datamaskinen skal deretter ikke fornye
dem. Den kan være en Mac, en Windows-PC eller en Linux-maskin med skrivebord.
Prøven over ble gjort fra en Mac; utrulling og flytting fra Windows og Linux er
ikke prøvd ennå.

## Kostnad og personvern

Fly.io gir container, vedvarende disk, HTTPS, hemmeligheter og start ved kall.
Oppsettet bruker én shared-cpu-1x-maskin med 256 MB og én disk på 1 GB. Den
stopper når den er ubrukt. Ingen vedlikeholdskall holder den våken. I prøven
stoppet Fly maskinen etter omtrent fem minutter uten kall. Neste kall startet
den igjen: maskinen var i gang etter omtrent 0,65 sekunder og svarte etter
omtrent 2,4 sekunder, og kallet lyktes uten feil i klienten.

Kontrollert 18. november 2026: [Fly-prisene](https://fly.io/pricing/) viser
2,19 USD per måned for kontinuerlig kjøring med 256 MB i billigste region og
2,28 USD i Stockholm. Disk koster 0,15 USD per GB og måned, og en stoppet
maskin koster 0,15 USD per GB rotfilsystem. Maskinen stopper når den er
ubrukt, så regningen blir normalt lavere. Nettverk, sikkerhetskopier,
valutakurs og avgifter kommer eventuelt i tillegg. Under 30 kr i måneden er et
anslag, ingen prisgaranti.

Nye kontoer får 1 USD i [prøvekreditt](https://fly.io/docs/about/free-trial/)
i sju dager, uten betalingskort. Når kreditten er brukt eller dagene er gått,
stopper maskinene til du legger inn kort. Fly skriver at regninger under 5 USD
i måneden frafalles for personlige kontoer. Det kan gjøre verten gratis i
praksis, men Fly kan endre det. Ikke baser driften på det. Se også
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

1. Installer `flyctl` etter [Flys veiledning](https://fly.io/docs/flyctl/install/),
   på Mac for eksempel med `brew install flyctl`.
2. Opprett selv Fly-kontoen og godkjenn kostnaden. Kjør `flyctl auth login`.
3. Kjør på datamaskinen der du logget inn:

   ```sh
   familiekilder-mcp deploy fly --app DITT-UNIKE-APPNAVN
   ```

Kommandoen lager app og disk, setter hemmeligheter via stdin og bygger fra
kildeutsjekkingen på Flys byggetjeneste. Den bruker nøyaktig én maskin, aldri en reservemaskin.
Utrulling skjer med `immediate`, som gir et kort avbrudd. En eksisterende app
med flere maskiner avvises før endringer. Installasjons-ID-en endres ikke ved
ny utrulling. `--region arn` og `--org personal` er standard.

Standardbildet kan ikke lese Word-vedlegg (.docx og .doc). Vil du det, legg
til `--word`:

```sh
familiekilder-mcp deploy fly --app DITT-UNIKE-APPNAVN --word
```

Da bygges bildet med LibreOffice, og maskinen får 512 MB vekselminne (swap).
Bildet blir omtrent 2,5 ganger så stort, og utrullingen tar lengre tid. Valget
huskes ikke. Ruller du ut senere uten
`--word`, forsvinner Word-støtten igjen. Kommandoen sier derfor ved hver
utrulling om Word-vedlegg er på eller av. Se
[Word-vedlegg på verten](#word-vedlegg-på-verten) for hva som er prøvd.

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
abonnementer og utrulling kan endres. Claude og ChatGPT på nett er prøvd med
denne serveren; se [Prøvd på ekte vert](#prøvd-på-ekte-vert).

**Claude:** [egne koblinger](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)
finnes også på Free, begrenset til én. Legg til under **Customize > Connectors
> + Add > Add custom connector** på nett. Bruk vertens `/mcp`-adresse,
OAuth-innlogging og automatisk klientregistrering der valget vises. Skriv
eierkoden bare på vertens godkjenningsside. Claude dokumenterer at eksterne
koblinger brukes gjennom skyen også fra mobil. Koblingen er prøvd både på
nett og i iPhone-appen. I appen ligger den under **+ > Tilkoblinger**, og
hvert verktøy spør om tillatelse første gang.
[Autentisering](https://claude.com/docs/connectors/building/authentication)
støtter DCR, som denne serveren bruker. CIMD annonseres ikke.

**ChatGPT:** Åpne **Plugins > + > Add custom MCP server** på nett, oppgi
adressen, velg OAuth og gjennomgå risikovarselet. Utviklermodus skal ikke slås
på først i den nåværende Plugins-flyten. Etter godkjenning: installer og bruk
koblingen i en ny samtale. [MCP-veiledningen](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)
dokumenterer full MCP for Business og Enterprise/Edu og lesende MCP for Pro.
[Plugins-veiledningen](https://help.openai.com/en/articles/20001256-plugins-in-chatgpt)
og kontoreglene må kontrolleres før kjøp: en synlig katalog beviser ikke at
din plan kan legge til en egen MCP-server. Koblingen er prøvd på en
Pro-konto; Plus, Free og Go loves ikke her. MCP-siden sier at tilkobling
opprettes på nett, ikke mobil. En kobling som er lagt til på nett, virket
deretter i iPhone-appen: velg **+ > Pluginer** og søk etter navnet. En
planlagt oppgave i ChatGPT er også prøvd; se
[Prøvd på ekte vert](#prøvd-på-ekte-vert).

Begge klientene kan registrere OAuth-klienter automatisk. Registrering gir
ingen tilgang før eierkoden godkjennes. Serveren annonserer `offline_access`
og gir roterende refresh-token. Se [OpenAIs autentiseringskrav](https://developers.openai.com/plugins/build/auth).
Access-token varer én time; refresh-token varer inntil 90 dager fra siste
fornyelse. Dette forlenger ikke økten hos skolen eller barnehagen.

## En kveldsoppgave i skyen

[Claude Cowork](https://support.claude.com/en/articles/13854387-schedule-recurring-tasks-in-claude-cowork)
dokumenterer planlagte skyoppgaver med koblinger på betalte planer, også når
datamaskinen sover. Free-kobling betyr altså ikke gratis planlagt kjøring. Velg en
skyoppgave uten lokal mappe eller «Require this computer». Koble til verten og
eventuelt kalenderen separat, og prøv først oppgaven manuelt:

> Hver kveld kl. 19: les nye beskjeder og aktiviteter gjennom Familiekilder.
> Oppsummer hva vi må gjøre i morgen, og ta med frister. Si tydelig fra om en
> kilde ikke kunne leses. Ikke regn access_expired alene som tapt tilgang.

Kalenderendringer krever en egen kalenderkobling og en uttrykkelig bestilling.
Kontroller én planlagt kjøring med datamaskinen avslått før du stoler på
rutinen.
En planlagt skykjøring fra Claude Code og en planlagt oppgave i ChatGPT er
prøvd mot en ekte vert; se [Prøvd på ekte vert](#prøvd-på-ekte-vert).
Planlagte oppgaver i Claude Cowork er ikke prøvd; det gis ikke et løfte om
dem.

## Prøvd på ekte vert

Den første ekte prøven ble gjort 17. og 18. november 2026 på Fly.io i region `arn`,
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
  kontoen har på nett. Dagen etter ble en hel samtale kjørt i
  nettgrensesnittet: Claude spurte om tillatelse for hvert verktøy («Allow
  once»), kalte `family_connections` og `spond_list_items` og svarte riktig.
- **ChatGPT på nett.** På en Pro-konto ble verten lagt til med **Plugins >
  Add > Add custom MCP server**, med OAuth og uten utviklermodus.
  Risikovarselet «Custom MCP servers introduce risk» måtte godtas. Vertens
  godkjenningsside viste appen som «ChatGPT». Etter eierkoden og «Godkjenn»
  gikk nettleseren tilbake til chatgpt.com, og koblingen viste 20
  leseverktøy. En ny samtale kalte `family_connections` og
  `spond_list_items` uten spørsmål om tillatelse og svarte riktig.
- **Claude og ChatGPT på iPhone.** Koblingene var lagt til på nett dagen før.
  I Claude-appen (1.261113.19) lå «Familiekilder» under **+ > Tilkoblinger**.
  Appen spurte om tillatelse for hvert verktøy («Tillat én gang», «Tillat
  alltid», «Avslå»), kalte `family_connections` og `spond_list_items` og
  svarte riktig. I ChatGPT-appen (1.2026.307) ble koblingen valgt med
  **+ > Pluginer** og søk etter navnet. Appen kalte verktøyene uten spørsmål
  om tillatelse og svarte riktig. Ingen av appene ba om ny innlogging.
- **Planlagt skykjøring.** En planlagt oppgave («routine») i Claude Code på
  nett, med bare denne koblingen, ble satt til å kjøre én gang noen minutter
  frem i tid. Den startet i Anthropics sky til avtalt tid, kalte
  `family_connections`, `spond_list_items`, `hoopit_list_items` og
  `gmail_search`, og skrev en oppsummering. Kjøringen tok 35 sekunder og
  brukte ikke Macen. Macen var likevel på, så en kjøring med Macen avslått er
  ikke prøvd bokstavelig.
- **Planlagt oppgave i ChatGPT.** I en samtale med koblingen valgt ble ChatGPT
  bedt om å lage en oppgave som skulle kjøre én gang seks minutter senere.
  ChatGPT laget oppgaven og viste den under **Scheduled**. Den kjørte til
  avtalt tid mens Macens skjerm var låst, og svaret var riktig. ChatGPT viser
  ikke verktøykall for en planlagt kjøring, men verten registrerte en lesing
  fra Spond i samme minutt som svaret er stemplet.
- **Flytting av en utløpt innlogging.** Gmail ble flyttet med
  `remote move ADRESSE --source gmail`. Google-innloggingen viste seg å være
  utløpt hos Google. Verten svarte da «Gmail-tilgangen må fornyes med
  Google-innlogging» på `gmail_search`, og de andre kildene virket som før.
  Flyttingen tar også med Google-klienten. Ny innlogging på datamaskinen
  krever derfor Google-filen på nytt; ta vare på den.
- **Word-vedlegg.** Se [Word-vedlegg på verten](#word-vedlegg-på-verten).
  Uten vekselminne gikk maskinen tom for minne. `deploy fly --word` gir den
  derfor vekselminne.
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

- Claude og ChatGPT på Android.
- Planlagte oppgaver i Claude Cowork.
- Kilder som logger inn med BankID (Vigilo, Skolemelding, IST Home og Flyt)
  på verten, og Gmail med en gyldig innlogging. Bare Spond og Hoopit leses
  der nå.
- Ekte Word-vedlegg fra en kilde.
- Fornyelse over dager og uker, og hva verten faktisk koster per måned.

## Når en innlogging må fornyes

Logg inn på nytt i veiviseren på datamaskinen, og kjør `remote move ADRESSE`
igjen.
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
beholdes sperren. **Verste utfall er ny innlogging på datamaskinen.** Ikke fjern
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
modus 0700. LibreOffice er et valgfritt byggargument `WITH_LIBREOFFICE=1`;
se [Word-vedlegg på verten](#word-vedlegg-på-verten). Målt med Docker på arm64
17. november 2026 brukte bildet omtrent 49–51 MiB i ro etter OAuth og omstart,
både med og uten LibreOffice. Containerprøven bekreftet full SDK-OAuth,
verktøyliste, gjenbruk av token etter omstart, UID 10001 og datamodus 0700.

## Word-vedlegg på verten

Word-vedlegg gjøres om til PDF med LibreOffice og leses så som PDF.
Standardbildet har ikke LibreOffice, fordi det gjør bildet mye større. Uten
LibreOffice svarer lesingen «soffice er ikke installert». Slå det på med
`deploy fly --word`, eller med `--build-arg WITH_LIBREOFFICE=1` når du bygger
selv.

Bildestørrelse for linux/amd64, som Fly bruker, målt 18. november 2026:

| Bilde | Utpakket | Komprimert |
| --- | --- | --- |
| Uten LibreOffice | 218 MB | omtrent 78 MB |
| Med LibreOffice | 536 MB | omtrent 204 MB |

Komprimert størrelse er målt med `docker save` og gzip. `flyctl` oppga 69 MB
for standardbildet, så tallene der kan avvike litt. En stoppet maskin betaler
for rotfilsystemet; se [Kostnad og personvern](#kostnad-og-personvern).

**Prøvd på ekte vert 18. november 2026.** Maskinen har 256 MB, og Linux
oppga 215 MB som samlet minne. Med serveren i gang var omtrent 105 MB ledig.

- **Uten vekselminne virket det ikke.** Linux drepte LibreOffice («Out of
  memory», avslutningskode 137). I en hardere prøve drepte Linux selve
  serveren, og maskinen startet på nytt.
- **Med 512 MB vekselminne virket det.** `deploy fly --word` setter derfor
  `swap_size_mb = 512` i `fly.toml`. Et oppdiktet Word-dokument ble lest med
  `spond_read_attachment` og falske kildesvar, ved siden av den ekte serveren.
  Teksten kom med, også æ, ø og å. Omtrent 60 MB vekselminne var i bruk.
- **Det går tregt.** LibreOffice brukte 22 sekunder på én side og 30 sekunder
  på 25 sider. Grensen er 60 sekunder per konvertering, så svært store
  dokumenter kan feile. Lesing av PDF er ikke berørt.
- Ekte Word-vedlegg fra en kilde er ikke prøvd; kildene på prøveverten hadde
  ingen.

Vekselminne koster ikke noe ekstra hos Fly. Mer minne ville gått raskere, men
`deploy fly` har ikke et valg for det.

**Prøvd i lokal container.** Prøven ble gjort 18. november 2026 i Docker på
en Mac med Apple-brikke. Bildet ble bygd for linux/amd64 og
kjørt gjennom emulering. Containeren fikk 256 MB minne, ingen swap og én CPU,
som Fly-maskinen. Et oppdiktet Word-dokument ble lest med
`spond_read_attachment` og falske kildesvar, som brukeren `familiekilder`.

- **Det virket.** Teksten kom med, også æ, ø og å, og siden kom som bilde.
  Et dokument på 25 sider virket også.
- **Tid.** LibreOffice brukte omtrent 1,4–1,8 sekunder. Hele lesingen av et
  dokument på én side tok omtrent 2 sekunder.
- **Minne.** Ingen prosess ble drept av minnegrensen, verken med 256 MB eller
  512 MB. Med 256 MB nådde containeren likevel taket. Serveren og LibreOffice
  brukte opptil 255 MB, og Linux måtte frigjøre bufret programkode underveis.
  Med 512 MB var det god margin. På arm64 uten emulering var toppen omtrent
  207 MB.
- **Mapper.** LibreOffice trenger en skrivbar `/tmp` eller `/var/tmp`, og det
  har Fly. Hjemmemappen trenger ikke være skrivbar. LibreOffices midlertidige
  filer legges i vedleggets private arbeidsmappe og slettes med den.

256 MB holdt altså i den lokale containeren, men ikke på den ekte maskinen,
der også Linux-kjernen bruker av de samme 256 MB. Bygger du bildet selv for en
annen vert, trenger den mer enn 256 MB minne eller vekselminne. Feiler
lesingen med «soffice avsluttet med -9», eller starter maskinen på nytt under
lesing, var minnet for lite.

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
  Kommandoen trenger en maskin som er i gang. Har Fly stoppet den, svarer
  `flyctl` «has no started VMs». Åpne da `https://APP.fly.dev/healthz` i
  nettleseren for å starte den, og kjør kommandoen på nytt innen noen
  minutter.
  `--client ID` trekker bare tilbake én klient. Endringen gjelder neste kall.
  Ny eierkode lages med `deploy fly --app APP --new-owner-code`; tilbakekall
  også eksisterende tokens ved mistanke om misbruk. Ikke mist koden før ny
  utrulling er fullført.
