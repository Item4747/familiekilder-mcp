# Kom i gang med familiekilder-mcp

[Tilbake til forsiden](../README.md).

Denne veiledningen er for førstegangsoppsett og vedlikehold. Oppsettet krever
terminalbruk og klientkonfigurasjon for tjenestene du vil koble til. Be gjerne
om hjelp fra noen med teknisk erfaring.

## Installere

Du trenger macOS, Python 3.11 eller nyere og [Brave](https://brave.com/) for
innlogging i nettleser. Lesing av PDF krever Poppler (`pdftotext`, `pdftoppm`),
og Word-dokumenter krever LibreOffice. Prosjektet er laget for og prøvd på macOS:
passord lagres i macOS-nøkkelringen, og standard datarot er
`~/Library/Application Support/familiekilder-mcp`.

Last ned kildekoden fra reposiden (**Code → Download ZIP**) og pakk den ut.
Åpne Terminal og gå til mappen du pakket ut; bytt ut stien i første linje:

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

## Oppsett i nettleseren

Kjør `familiekilder-mcp setup`, eller `scripts/setup` fra prosjektmappen.
Sistnevnte installerer pakken først. Veiviseren åpnes på `127.0.0.1` med en
ledig port og er bare tilgjengelig på denne maskinen.

1. Velg tjenestene du bruker. Spond klargjøres automatisk ved innlogging. IST
   Home kan hente oppsettet fra en innlogget Mac-app. Gmail trenger din egen
   Google Desktop-klient; øvrige klientfiler velger du fra maskinen.
   Se [klientoppsett](klientoppsett.md).
2. Logg inn i det separate nettleservinduet. BankID gjør du selv. For Spond og
   Hoopit trykker du «Jeg er logget inn» når tjenesten er ferdig lastet.
3. Velg Claude Desktop eller lokal ChatGPT / Codex. Veiviseren endrer bare
   oppføringen for familiekilder-mcp og beholder øvrige klientinnstillinger.
4. Test MCP-forbindelsen og start AI-klienten på nytt. Testen henter ingen
   beskjeder; den kontrollerer den lokale serveren og leseverktøyene.

Avslutt med «Avslutt oppsett» eller Ctrl+C. Innloggingen og konfigurasjonen
beholdes. AI-klienten starter MCP-serveren ved senere bruk. `setup --no-open`
viser adressen uten å åpne nettleseren; `--port NUMMER` velger en fast port.

Claude-konfigurasjonen lagres i `~/Library/Application Support/Claude/`.
Lokal ChatGPT / Codex bruker `~/.codex/config.toml`. Har du valgt en annen
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

Før innlogging må du legge inn [klientkonfigurasjon](#klientkonfigurasjon-per-tjeneste).
Det er et eget oppsett for integrasjonen, i tillegg til brukerkontoen din.
Deretter logger du inn:

```sh
familiekilder-mcp auth vigilo browser-login        # Vigilo, Skolemelding, IST Home, Flyt
familiekilder-mcp auth spond browser-bootstrap     # Spond, Hoopit
familiekilder-mcp auth spond import-session --session-file /privat/mappe/spond-økt.json
familiekilder-mcp auth gmail login                 # se docs/gmail.md
```

Innlogging i nettleser åpner en isolert, midlertidig Brave-profil. BankID og
telefonbekreftelse gjør du selv. For Vigilo, Skolemelding, IST Home og Flyt ber
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

Spond klargjøres lokalt i veiviseren uten en egen oppsettsfil. For andre
tjenester trenger du klientoppsett i tillegg til brukerkontoen din. Prosjektet
deler ikke ut klienthemmeligheter eller signerte providerpakker. Legg inn
klientoppsettet for én tjeneste om gangen:

```sh
familiekilder-mcp auth <tjeneste> configure --client-file FIL
```

Filen er privat JSON (filmodus 0600, i en privat mappe) og skal aldri ligge i
Git. Feltene kontrolleres av `configure_client` i tjenestens auth-modul
(`vigilo_auth.py`, `skolemelding_auth.py`, `spond_auth.py`, `hoopit_auth.py`
og `ist_oauth.py`).
For IST Home kan `familiekilder-mcp ist-auth export-mac-client --output FIL` hente
klientkonfigurasjonen fra IST Home-appen på Macen din. Gmail bruker din egen
Google OAuth-klient av typen Desktop app; se [Gmail](gmail.md).

Den utgitte pakken har ingen betrodde leverandørnøkler og kan derfor ikke
installere en signert providerpakke uten at prosjektets nøkkelliste først
endres. Bruk separat klientkonfigurasjon i standardoppsettet.

## Profiler og passord

- `--profile NAVN` gir en egen profil med egne tjenester, økter og passord, uten
  arv fra standardprofilen. `--root MAPPE` velger en annen datarot.
- `familiekilder-mcp password set bankid` lagrer et passord i macOS-nøkkelringen, så
  innloggingen i nettleseren kan fylle det ut. Det finnes ingen filbasert reserve.

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
kommandoen, feilmeldingen og hvilken tjeneste det gjelder, uten å dele
innlogging, tokens, navn eller innhold fra ekte beskjeder.

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

