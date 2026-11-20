# Prøve på Windows eller Linux

[Tilbake til forsiden](../README.md) · [Kom i gang](kom-i-gang.md)

Denne siden er for deg som vil prøve familiekilder-mcp på Windows eller Linux,
eller med Chrome eller Edge. Det er nytt. Installasjonen, veiviseren og
serveren er prøvd maskinelt på alle tre systemene, men ingen har ennå logget
inn hos en ekte tjeneste på Windows eller Linux. Se [hva som er
prøvd](#hva-som-er-prøvd).

Begynn gjerne med én tjeneste, for eksempel Spond eller Hoopit. Si fra under
**Issues** på reposiden både når det virker og når det ikke gjør det.

## Hva du trenger

**Windows 10 eller 11**

- Python 3.11 eller nyere fra
  [python.org](https://www.python.org/downloads/windows/). Installasjonen tar
  med `py`, som kommandoene under bruker.
- En nettleser for innlogging. Microsoft Edge følger med Windows og holder.
  Brave og Chrome virker bare når de er installert **for alle brukere**, under
  `Program Files`. Er de installert bare for deg, under `AppData`, hopper
  programmet over dem og bruker Edge. Forhåndssjekken sier hvorfor.
- Claude Desktop eller ChatGPT-appen med Codex.

**Linux med skrivebord**

- Python 3.11 eller nyere med `venv`. På Debian og Ubuntu kommer `venv` i en
  egen pakke: `sudo apt install python3-venv`.
- Brave, Google Chrome eller Microsoft Edge fra leverandørens egen .deb- eller
  .rpm-pakke. Snap, Flatpak og distribusjonens `chromium` blir avvist.
- Et skrivebord med skjerm. Innloggingen virker ikke over SSH.
- Valgfritt: GNOME Keyring eller KWallet hvis du vil lagre BankID-passordet.
  Uten nøkkelring skriver du passordet selv i nettleservinduet.
- Claude Desktop eller Codex.

## Kommandoene

Last ned kildekoden fra reposiden (**Code → Download ZIP**) og pakk den ut.
Har stien mellomrom, setter du den i enkle anførselstegn i `cd`-linjen.

**Windows**, i PowerShell:

```powershell
cd 'C:\sti\til\familiekilder-mcp'
py scripts\setup.py
```

**Linux**, i Terminal:

```sh
cd '/sti/til/familiekilder-mcp'
scripts/bootstrap-local-venv
export PATH="$PWD/.private/venv/bin:$PATH"
familiekilder-mcp setup
```

Dette er de samme kommandoene som i [Kom i gang](kom-i-gang.md#last-ned-og-installer).

## Hva du skal se

1. **Installasjonen** skriver mye fra `pip` og slutter med
   `familiekilder-mcp er klart i …`. Første gang tar det et halvt til to
   minutter.
2. **Veiviseren** skriver `Åpne lokalt oppsett: http://127.0.0.1:…` og åpner
   seg i den vanlige nettleseren din. Åpnes den ikke, kopierer du adressen inn
   selv.
3. **Forhåndssjekken.** Åpne «Hva trenger jeg på maskinen?» i veiviseren og se
   på «Forhåndssjekk», eller kjør den fra en ny terminal; se
   [under](#forhåndssjekken). Første linje skal være `Resultat: OK`.
4. **Logg inn.** Velg «Logg inn» på én tjeneste. Et eget nettleservindu
   åpnes med en tom, midlertidig profil. Logg inn der. For Spond og Hoopit
   trykker du «Jeg er logget inn» i veiviseren når tjenesten er ferdig lastet.
   Veiviseren skal vise at innloggingen er lagret.
5. **AI-assistenten.** Velg Claude Desktop eller ChatGPT / Codex i steg 2.
6. **Prøv forbindelsen** i steg 3. Den skal si at den lokale forbindelsen
   virker. Den henter ingen beskjeder.
7. **Start AI-appen på nytt** og spør for eksempel: «Hva har kommet fra Spond
   den siste uken?»

Avslutt veiviseren med «Avslutt oppsett» eller Ctrl+C i terminalen.

## Forhåndssjekken

Forhåndssjekken ser over maskinen uten å logge inn eller gjøre nettverksoppslag.
Den viser:

- versjon, system og Python;
- hvilke nettlesere som ble funnet, hvilken som er første kandidat, og
  hvorfor en nettleser eventuelt ble avvist, med hva du kan gjøre;
- om passordlagring er tilgjengelig;
- om den private datamappen har riktig eier og tilgang;
- om Claude Desktop og Codex har familiekilder-mcp registrert;
- om det finnes en skjerm (Linux);
- hvilke tjenester som er slått på, og om de har lagret innlogging.

Kjør den fra en ny terminal i prosjektmappen:

```powershell
.\.private\venv\Scripts\familiekilder-mcp.exe doctor
```

```sh
.private/venv/bin/familiekilder-mcp doctor
```

På Windows står signaturen som uavklart til innloggingen kjører Authenticode.
På Linux gjør forhåndssjekken den samme lokale pakkekontrollen som ved
innlogging. Se [hva som sjekkes](kom-i-gang.md#forhåndssjekk).

Rapporten viser hvordan dataroten er valgt og om den er i orden. Stien til
dataroten, profilnavn og innhold fra tjenestene utelates. Les rapporten før
du deler den.

## Hvis noe går galt

Opprett en sak under **Issues** og lim inn:

1. Hele rapporten fra forhåndssjekken.
2. Hva du gjorde, og hvilket steg det stoppet på.
3. Feilmeldingen, fra veiviseren eller de siste linjene i terminalen.
4. Nettleser og tjeneste.

Ikke del innlogging, passord, tokens, navn på barn eller innhold fra ekte
beskjeder. Står det noe slikt i en feilmelding, bytt det ut med `…`.

Viser veiviseren bare «Steget kunne ikke fullføres», kan du logge inn fra
terminalen for å se hele feilmeldingen. Veiviseren må ha prøvd minst én gang
først, så tjenesten har oppsett. Kjør bare linjen for tjenesten du prøver.

**Windows (PowerShell)**

```powershell
.\.private\venv\Scripts\familiekilder-mcp.exe auth spond browser-bootstrap
.\.private\venv\Scripts\familiekilder-mcp.exe auth hoopit browser-bootstrap
.\.private\venv\Scripts\familiekilder-mcp.exe auth vigilo browser-login
```

**Linux (Terminal)**

```sh
.private/venv/bin/familiekilder-mcp auth spond browser-bootstrap
.private/venv/bin/familiekilder-mcp auth hoopit browser-bootstrap
.private/venv/bin/familiekilder-mcp auth vigilo browser-login
```

For Skolemelding, IST Home eller Flyt bytter du ut `vigilo` med henholdsvis
`skolemelding`, `ist-home` eller `flyt` på den siste linjen.

## Hva som er prøvd

Maskinelle prøver kjører på Linux for hver endring. På Windows og macOS kjører
de når en endring kan virke ulikt der, og før en ny versjon. De bruker bare
oppdiktede data og lokale sider og kontakter ingen tjeneste.

| Del | Hvordan det er prøvd |
|---|---|
| Installasjon slik denne veiledningen beskriver den | Kommandoene fra [Kom i gang](kom-i-gang.md#last-ned-og-installer), ordrett, med en tom hjemmemappe der stien har mellomrom og æøå. På Windows i Windows PowerShell 5.1 (`scripts/install_trial.py`). |
| Veiviseren | Startet fra installasjonen, kobler til Claude Desktop og Codex og tester forbindelsen. |
| Serveren slik AI-appen starter den | Kommandoen fra klientkonfigurasjonen, uten UTF-8-modus, mot en oppdiktet Spond med æøå, «», tankestrek, emoji og vedlegg. Svarene er gyldig UTF-8. På Windows også med tegnsett 850 og 1252 (`scripts/stdio_trial.py`). |
| Utskrift i eldre konsoller | Alle kommandoer med tegnsett 850 og 1252 (`tests/test_console.py`). |
| Nettleserne | Brave, Chrome og Edge starter i en midlertidig profil og svarer over DevTools (`scripts/browser_smoke.py`). |
| Innloggingsløpet | Med ekte nettleser og en oppdiktet innloggingsside på 127.0.0.1: tilbakekallet fanges, koden byttes hos en lokal, oppdiktet tokentjeneste, økten lagres privat og leses første gang. Spond- og Hoopit-økter leses fra en side i nettleseren (`scripts/login_trial.py`). Hvor prøven skiller seg fra en ekte innlogging, står øverst i skriptet. |
| Nettlesere installert per bruker (Windows) | Brave og Chrome under `AppData` hoppes over, Edge brukes, og forhåndssjekken forklarer hvorfor. |
| Passordlagring | Et oppdiktet passord lagres, leses og slettes i Legitimasjonsbehandling (Windows) og GNOME Keyring (Linux). Uten nøkkelring sier programmet fra, og innloggingen virker uten lagret passord (`scripts/password_trial.py`). |
| Ekte innlogging hos tjenestene | Bare på Mac med Brave. |

Det som bare et menneske med ekte konto kan prøve: innloggingssidene hos
tjenestene, BankID, at økten virker mot tjenestens ekte svar, og at
AI-appen bruker verktøyene.
