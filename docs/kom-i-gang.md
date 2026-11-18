# Kom i gang med familiekilder-mcp

[Tilbake til forsiden](../README.md).

Denne veiledningen er for førstegangsoppsett og vedlikehold. Oppsettet krever
litt terminalbruk. Be gjerne om hjelp fra noen med teknisk erfaring.

## Installere

Programmet kan brukes på macOS, Windows og Linux. Du trenger Python 3.11 eller
nyere og en nettleser for innlogging: Brave, Google Chrome eller Microsoft
Edge; se [nettleseren](#nettleseren). Lesing av PDF krever Poppler
(`pdftotext`, `pdftoppm`), og Word-dokumenter krever LibreOffice.

### Hva er prøvd

- **Mac med Brave** er prøvd med ekte innlogging hos tjenestene.
- **Nytt:** Chrome og Edge på Mac, og alle tre nettleserne på Windows og
  Linux. Det er prøvd maskinelt, ikke med ekte innlogging ennå. På alle tre
  systemene kjører prøvene installasjonen ordrett etter denne veiledningen,
  veiviseren, serveren slik AI-appen starter den, nettleserne og
  innloggingsløpet mot oppdiktede, lokale sider. Lagrede passord er prøvd mot
  Legitimasjonsbehandling på Windows og GNOME Keyring på Linux. Ingen prøve
  logger inn hos en ekte tjeneste.

Vil du prøve på Windows eller Linux, les [Prøve på Windows eller
Linux](prove-pa-windows-og-linux.md). Der står hva du trenger, hva du skal se
i hvert steg, hva hver maskinell prøve dekker, og hva du limer inn i en sak
hvis noe går galt. Si gjerne fra under **Issues**, også når det virker.

### Last ned og installer

Last ned kildekoden fra reposiden (**Code → Download ZIP**) og pakk den ut.

**macOS og Linux.** Åpne Terminal og gå til mappen du pakket ut; bytt ut stien
i første linje. Har stien mellomrom, setter du den i enkle anførselstegn. På
Debian og Ubuntu trenger du også pakken `python3-venv`.

```sh
cd /sti/til/familiekilder-mcp
scripts/bootstrap-local-venv
export PATH="$PWD/.private/venv/bin:$PATH"
```

Skriptet lager `.private/venv` og installerer pakken der, med versjonene som er
låst i `pyproject.toml`. Kommandoen er da
`.private/venv/bin/familiekilder-mcp`. `export`-linjen gjør kommandoen
tilgjengelig i dette terminalvinduet. Den gamle kommandoen `familiekilder`
virker også.

**Windows.** Installer Python 3.11 eller nyere fra
[python.org](https://www.python.org/downloads/windows/). Åpne PowerShell og gå
til mappen du pakket ut. Har stien mellomrom, setter du den i enkle
anførselstegn: `cd 'C:\sti med mellomrom\familiekilder-mcp'`.

```powershell
cd C:\sti\til\familiekilder-mcp
py scripts\setup.py
$env:Path = "$PWD\.private\venv\Scripts;$env:Path"
```

`scripts\setup.py` lager `.private\venv`, installerer pakken der og åpner
veiviseren. Kommandoen er da `.private\venv\Scripts\familiekilder-mcp.exe`.
Når du har avsluttet veiviseren, gjør den siste linjen kommandoen
tilgjengelig i dette vinduet.

### Hvor dataene ligger

Innlogginger, oppsett og mellomlager ligger i dataroten, utenfor koden:

| System | Standard datarot |
|---|---|
| macOS | `~/Library/Application Support/familiekilder-mcp` |
| Windows | `%LOCALAPPDATA%\familiekilder-mcp` |
| Linux | `~/.local/share/familiekilder-mcp`, eller `$XDG_DATA_HOME/familiekilder-mcp` |

`--root MAPPE` eller `FAMILIEKILDER_ROOT` velger en annen. Mappen `.private`
er bare tilgjengelig for brukeren din: på macOS og Linux med filrettigheter, på
Windows med en tilgangsliste. Mappen må også eies av deg. Har en annen bruker
på maskinen laget den, stopper programmet; flytt den bort eller velg en annen
datarot.

## Nettleseren

Innlogging i nettleser åpner en isolert, midlertidig profil i Brave, Google
Chrome eller Microsoft Edge. Profilen slettes etterpå, og den bruker ikke
bokmerker, innlogginger eller utvidelser fra din vanlige nettleser. Programmet
styrer vinduet gjennom en privat forbindelse, aldri gjennom en nettverksport.
Gmail er unntaket: Google-innloggingen åpnes i den vanlige nettleseren din; se
[Gmail](gmail.md).

Programmet bruker bare en nettleser som er installert fra leverandøren, og
kontrollerer den før hver start. En nettleser som ikke består kontrollen,
brukes ikke.

- **macOS:** Nettleseren må ligge i `/Applications` eller `~/Applications`.
  Programmet kontrollerer signaturen fra leverandøren og at macOS godtar appen.
- **Windows:** Installer nettleseren **for alle brukere** på maskinen, så den
  havner under `Program Files` eller `Program Files (x86)`. Det krever
  administrator. En nettleser som bare er installert for deg, under `AppData`,
  blir avvist. Det er vanlig for Chrome og Brave når de installeres uten
  administratorrettigheter. Programmet kontrollerer også signaturen fra
  leverandøren, og avviser en mappe som vanlige brukere kan endre.
- **Linux:** Bruk leverandørens egen .deb- eller .rpm-pakke fra
  [brave.com](https://brave.com/linux/),
  [google.com/chrome](https://www.google.com/chrome/) eller
  [microsoft.com/edge](https://www.microsoft.com/edge). Pakkene legger
  nettleseren under `/opt`. Programmet kontrollerer hele mappen: alle filer
  der må eies av root, ingen andre må kunne endre dem, og ingen lenker må peke
  ut av mappen. Snap, Flatpak og distribusjonens `chromium`
  støttes ikke. Innloggingen trenger et skrivebord med skjerm. Over SSH eller
  på en server uten skjerm stopper den med en melding.

Har du ikke valgt, bruker innloggingen den første av Brave, Chrome og Edge,
i den rekkefølgen, som består kontrollen. En nettleser som feiler kontrollen,
eller som er installert på en måte som ikke støttes (bare for deg under
`AppData` på Windows; Snap, Flatpak eller distribusjonens Chromium på Linux),
hoppes over. Du får en linje om hvilken som ble hoppet over og hvorfor. Har du
valgt en nettleser, prøves bare installasjoner av den nettleseren, i samme
rekkefølge som ved automatisk valg. Hvis ingen av dem består, sier
feilmeldingen hvorfor og hva du kan gjøre. Velg i veiviseren under «Nettleser for
innlogging», eller fra terminalen:

```sh
familiekilder-mcp browser            # vis installerte nettlesere og valget
familiekilder-mcp browser chrome     # bruk Chrome; også brave, edge eller auto
```

`auto` fjerner valget. Valget gjelder profilen du bruker. Med
`--browser brave|chrome|edge` på `auth … browser-login` og
`auth … browser-bootstrap` velger du for én innlogging.

### Forhåndssjekk

```sh
familiekilder-mcp doctor           # rapport til skjermen
familiekilder-mcp doctor --json    # det samme som JSON
```

Forhåndssjekken bruker lokale opplysninger. Rapporten viser nettleserne,
passordlagring, statusen til den private datamappen, AI-klientene, skjerm på
Linux og hvilke tjenester som er slått på. For dataroten vises bare hvordan
den ble valgt og statusen, også ved feil. Profilnavn og egendefinerte stier
utelates. Den samme rapporten står under «Hva trenger jeg på maskinen?» i
veiviseren og kan limes inn i en sak.

- **macOS:** sjekker appstien og leser app- og utgiver-ID fra signaturen
  med `codesign -dv`. Gatekeeper (`spctl`) kjøres ved innlogging, før
  nettleseren startes.
- **Windows:** sjekker programstien, lenker og skrivetilgang med lokale
  tilgangslister. Authenticode-signaturen og utgiveren sjekkes ved
  innlogging, før nettleseren startes.
- **Linux:** sjekker pakkestien, eier, rettigheter og lenker i hele
  nettleserpakken lokalt, som ved innlogging.

På Mac og Windows står det «KANDIDAT» ved første aktuelle installasjon og
«UAVKLART» ved andre aktuelle nettlesere. Innloggingen avgjør hvilken
installasjon som består signaturkontrollen. Avslutningskoden er 1 ved en
konkret feil som blokkerer innlogging.

## Oppsett i nettleseren

Kjør `familiekilder-mcp setup`, eller fra prosjektmappen
`python3 scripts/setup.py` (på Windows `py scripts\setup.py`). Skriptet
installerer pakken først. `scripts/setup` virker også på macOS og Linux.
Veiviseren åpnes på `127.0.0.1` med en ledig port og er bare tilgjengelig på
denne maskinen.

1. Velg «Logg inn» på tjenestene du bruker. Vigilo, Skolemelding, Spond,
   Hoopit og IST Home har innebygd oppsett, så du trenger ingen fil. Flyt spør
   om kommune. Gmail trenger din egen Google-klient; velg den med «Velg
   Google-fil». Valgte du feil kommune eller fil, kan du velge på nytt så lenge
   du ikke har logget inn: kommunen i innloggingsdialogen, Google-filen med
   «Velg en annen Google-fil». Se [klientoppsett](klientoppsett.md).
2. Logg inn i det separate nettleservinduet. BankID gjør du selv. For Spond og
   Hoopit trykker du «Jeg er logget inn» når tjenesten er ferdig lastet.
3. Velg Claude Desktop eller lokal ChatGPT / Codex. Veiviseren endrer bare
   oppføringen for familiekilder-mcp og beholder øvrige klientinnstillinger.
4. Test MCP-forbindelsen og start AI-klienten på nytt. Testen henter ingen
   beskjeder; den kontrollerer den lokale serveren og leseverktøyene.

Avslutt med «Avslutt oppsett» eller Ctrl+C. Innloggingen og konfigurasjonen
beholdes. AI-klienten starter MCP-serveren ved senere bruk. `setup --no-open`
viser adressen uten å åpne nettleseren; `--port NUMMER` velger en fast port.

Claude Desktop-konfigurasjonen lagres i `claude_desktop_config.json` i
`~/Library/Application Support/Claude/` på macOS, `%APPDATA%\Claude\` på
Windows og `~/.config/Claude/` på Linux. Lokal ChatGPT / Codex bruker
`~/.codex/config.toml` på alle systemene. Har du valgt en annen
Codex-konfigurasjonsmappe, må du legge til serveren der manuelt.
Konfigurasjoner som ikke
kan leses sikkert, overskrives ikke. En lokal konfigurasjon betyr ikke at
vanlig ChatGPT-nettchat får tilgang til serveren.

## Oppsett fra terminalen

Slå på tjenestene du trenger. Deaktiverte tjenester registrerer ingen verktøy og
kontaktes ikke.

```sh
familiekilder-mcp configure spond                      # bare lesing
familiekilder-mcp configure hoopit --disable           # slå av igjen
```

Den selvstendige CLI-en og MCP-serveren tilbyr bare lesing. En eldre profil
med skriverettigheter får likevel bare leseverktøy; profilen skrives ikke om.

Innlogging fra terminalen krever at tjenesten har
[klientkonfigurasjon](#klientkonfigurasjon-per-tjeneste). Veiviseren legger inn
det innebygde oppsettet ved første innlogging; terminalen gjør ikke det.
Logg derfor inn første gang i veiviseren, eller importer en fil. Deretter:

```sh
familiekilder-mcp auth vigilo browser-login        # Vigilo, Skolemelding, IST Home, Flyt
familiekilder-mcp auth spond browser-bootstrap     # Spond, Hoopit
familiekilder-mcp auth spond import-session --session-file /sti/til/.private/spond-økt.json
familiekilder-mcp auth gmail login                 # se docs/gmail.md
```

En øktfil til `import-session` må ligge i en mappe som heter `.private` og
bare er tilgjengelig for deg. Innlogging i nettleser åpner en isolert,
midlertidig profil i nettleseren du har valgt; se [nettleseren](#nettleseren). BankID og telefonbekreftelse gjør du
selv. For Vigilo, Skolemelding, IST Home og Flyt ber
kommandoen deg første gang skrive `JEG FORSTÅR` for å bekrefte at integrasjonen
er uoffisiell.

Status og vedlikehold, uten å hente meldinger:

```sh
familiekilder-mcp connections               # alle tjenester: innlogging og rettigheter
familiekilder-mcp auth spond status         # én tjeneste, uten nettverk
familiekilder-mcp auth spond refresh        # forny økten nå
familiekilder-mcp auth spond health         # levetidshistorikk, uten nettverk
familiekilder-mcp auth spond logout
```

Start MCP-serveren (over stdio):

```sh
familiekilder-mcp mcp serve
```

Endringer i konfigurasjonen krever omstart av serveren.

## Koble til agenten din

Agenten starter serveren som en lokal prosess. Oppgi den absolutte stien til
kommandoen og argumentene `mcp serve`:

```sh
codex mcp add familiekilder-mcp -- /ABSOLUTT/STI/familiekilder-mcp/.private/venv/bin/familiekilder-mcp mcp serve
claude mcp add familiekilder-mcp -- /ABSOLUTT/STI/familiekilder-mcp/.private/venv/bin/familiekilder-mcp mcp serve
```

Andre MCP-klienter tar det samme som `command` og `args` i sin konfigurasjon.
Verktøyet `family_connections` viser agenten hvilke tjenester som er slått på,
og hver tjeneste får `<tjeneste>_list_items`, `<tjeneste>_get_item` og
`<tjeneste>_read_attachment` (Gmail har `gmail_search` i stedet for listen), pluss
tjenestens øvrige leseverktøy.

## Klientkonfigurasjon per tjeneste

Se [klientoppsett](klientoppsett.md) for hva hver kobling trenger og hvilke
oppsettsveier som finnes. Bruk gjerne `familiekilder-mcp --version` når du ber
om hjelp.

Vigilo, Skolemelding, Spond, Hoopit og IST Home har innebygd appidentitet, som
veiviseren legger inn når tjenesten mangler oppsett. Appidentiteten er lik for
alle brukere av leverandørens offisielle app og gir ikke tilgang til noen
konto. Flyt spør om kommune, og Gmail bruker din egen Google OAuth-klient av
typen Desktop app; se [Gmail](gmail.md).

Har du et eget klientoppsett, importerer du det for én tjeneste om gangen. Et
importert oppsett erstatter det som er lagret, også det innebygde:

```sh
familiekilder-mcp auth <tjeneste> configure --client-file FIL
```

Filen er privat JSON (filmodus 0600, i en privat mappe) og skal aldri ligge i
Git. Feltene kontrolleres av `configure_client` i tjenestens auth-modul
(`vigilo_auth.py`, `skolemelding_auth.py`, `spond_auth.py`, `hoopit_auth.py`
og `ist_oauth.py`).
For IST Home kan `familiekilder-mcp ist-auth export-mac-client --output FIL` hente
klientkonfigurasjonen fra IST Home-appen på en Mac.

Den utgitte pakken har ingen betrodde leverandørnøkler og kan derfor ikke
installere en signert providerpakke uten at prosjektets nøkkelliste først
endres. Bruk det innebygde oppsettet eller en egen klientfil.

## Profiler og passord

- `--profile NAVN` gir en egen profil med egne tjenester, økter og passord, uten
  arv fra standardprofilen. `--root MAPPE` velger en annen datarot.
- `familiekilder-mcp password set bankid` lagrer et passord i systemets
  nøkkelring, så innloggingen i nettleseren kan fylle det ut. Det er
  nøkkelringen på macOS, Legitimasjonsbehandling på Windows og Secret Service
  (GNOME Keyring eller KWallet) på Linux. Det finnes ingen filbasert reserve.
  Finnes ingen nøkkelring, er passordhjelperen av, og du skriver passordet i
  nettleservinduet. Innloggingen virker likevel.

Se [profiler og passord](personal-profiles.md).

## Personvern og uoffisielle koblinger

Programmet kjører lokalt. Innlogginger og mellomlagrede meldinger lagres i en
privat mappe med tilgang bare for OS-brukeren din. Det assistenten leser, kan
sendes til AI-leverandøren du har valgt. Assistenten står for oversettelse og
forklaring; denne pakken gir den tilgang til kildene.

Koblingene bruker tjenestenes egne API-er med din innlogging, men er ikke
godkjent av tjenestene. Gmail bruker Googles offisielle API med din egen
OAuth-klient. Du må følge tjenestenes vilkår. Endringer hos en leverandør kan
gjøre at en kobling slutter å virke.

## Begrensninger

- Lesing skjer når agenten spør. Det finnes ingen tidsplan, ingen bakgrunnshenting
  og ingen meldingsdatabase. Standardvinduet for lister er 14 dager, og tekstsøk
  filtrerer bare det aktuelle oppslaget.
- Hentede meldinger og vedlegg mellomlagres privat i opptil sju dager. Trafikken
  er begrenset lokalt til 1 000 forespørsler per time og 3 000 per døgn per
  tjeneste; se [mellomlagring](read-cache.md).
- Gmail kan bare lese, og bare med målrettede søk; se [Gmail](gmail.md).
- MCP-serveren tilbyr bare leseverktøy. Den sender ikke meldinger, melder
  fravær eller svarer på deltakelse.
- Hvor lenge en innlogging varer, bestemmer tjenesten. Det er ikke kjent for
  alle; se [fornyelse og levetid](auth-lifecycle.md).
- Store PDF-er leses side for side; se [trinnvis PDF-lesing](pdf-window-reading.md).

## Dokumentasjon

| Dokument | Innhold |
|---|---|
| [Prøve på Windows eller Linux](prove-pa-windows-og-linux.md) | Hva du trenger, hva du skal se, forhåndssjekken og hva som er prøvd |
| [Funksjonsmatrise](service-capabilities.md) | Lesing per tjeneste og grenser; intern kode for skriving er merket separat |
| [Fornyelse og levetid](auth-lifecycle.md) | Tokenfornyelse, lagring og hva som er målt |
| [Profiler og passord](personal-profiles.md) | Datarot, profiler og nøkkelring |
| [Gmail](gmail.md) | Søk, tempo og oppsett av egen OAuth-klient |
| [Mellomlagring](read-cache.md) | Cache, ferskhet og trafikkbudsjett |
| [Hoopit](hoopit-ingestion.md) | Oppslag og sideinndeling |
| [Flyt](flyt.md) | Kommunevalg, innlogging, ruter og grenser |
| [Trinnvis PDF-lesing](pdf-window-reading.md) | Vinduer, grenser og binding |
| [Legge til en ny tjeneste](adding-a-service.md) | Filer, kontrakter, registrering og tester |

## Hjelp og bidrag

Meld feil eller spørsmål under **Issues** på prosjektets reposide. Beskriv
kommandoen, feilmeldingen og hvilken tjeneste det gjelder, og lim inn
rapporten fra `familiekilder-mcp doctor`, uten å dele innlogging, tokens, navn
eller innhold fra ekte beskjeder.

### Kjøre testene

```sh
scripts/bootstrap-local-venv
scripts/test                          # alle testene, hver fil i egen prosess
scripts/test tests/test_spond.py      # bare én fil
uvx ruff@0.17.4 check .
```

Testene bruker bare oppdiktede data, falske transporter og midlertidige mapper.
De kontakter ingen ekte tjeneste; én test starter en lokal OIDC-server på
127.0.0.1. Regler for kode og for tilgang til ekte tjenester står i
[AGENTS.md](../AGENTS.md). Vil du koble til en ny tjeneste, start med
[veiledningen](adding-a-service.md).


## Egen vert som prøveordning

Lokal stdio er standard. For HTTP, OAuth, Fly.io og flytting av innlogginger,
se [alltid-på-oppsettet](alltid-pa.md). Innloggingen gjøres fortsatt på
datamaskinen din; ikke kopier øktfiler for hånd.
