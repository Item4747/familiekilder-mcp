# En vert som assistenten kan nå

Dette er en prøveordning utenfor løftet for 1.0. Lokal bruk på Mac over stdio
er fortsatt standard. HTTP-oppsettet må prøves med ekte Fly-konto, ekte
klienter og en flyttet økt før det kan omtales som bekreftet i daglig bruk.
Funksjonen er foreløpig bare i kildekoden, ikke i den publiserte 1.0.0rc2.

Verten kjører samme lesende MCP-server i en Linux-container. Den henter bare
når assistenten spør; den har ingen egen modell, kalenderkobling eller
planlegger. Du logger fortsatt inn i veiviseren på Macen, og flytter så
innloggingene til verten. Macen skal deretter ikke fornye dem.

## Kostnad og personvern

Fly.io gir container, vedvarende disk, HTTPS, hemmeligheter og start ved kall.
Oppsettet bruker én shared-cpu-1x-maskin med 256 MB og én disk på 1 GB. Den
stopper når den er ubrukt. Ingen vedlikeholdskall holder den våken.

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

Kommandoene under kjøres fra en utsjekking som inneholder funksjonen.
Installer først etter [oppsettsveiledningen](kom-i-gang.md), og logg inn i den
lokale veiviseren. Google-klienten for Gmail følger samme flytting som økten.

1. Installer `flyctl`, for eksempel med `brew install flyctl`.
2. Opprett selv Fly-kontoen og godkjenn kostnaden. Kjør `flyctl auth login`.
3. Kjør på Macen:

   ```sh
   familiekilder-mcp deploy fly --app DITT-UNIKE-APPNAVN
   ```

Kommandoen lager app og disk, setter hemmeligheter via stdin og bygger fra
kildeutsjekkingen. Den bruker nøyaktig én maskin, aldri en reservemaskin.
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

## Claude og ChatGPT

Faktaene her er kontrollert mot originalkilder 17. november 2026. Menyer,
abonnementer og utrulling kan endres. Ekte klienttilkobling med denne serveren
er fortsatt en egen prøve; SDK-testen erstatter den ikke.

**Claude:** [egne koblinger](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)
finnes også på Free, begrenset til én. Legg til under **Customize > Connectors
> + Add > Add custom connector** på nett. Bruk vertens `/mcp`-adresse,
OAuth-innlogging og automatisk klientregistrering der valget vises. Skriv
eierkoden bare på vertens godkjenningsside. Claude dokumenterer at eksterne
koblinger brukes gjennom skyen også fra mobil. Kontroller dette med egen
konto; denne koblingen er ikke prøvd i den ekte mobilklienten.
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
