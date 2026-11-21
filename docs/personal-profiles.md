# Personlige profiler og passord

Koden er felles. Hver bruker har sin egen datarot med tjenestekonfigurasjon,
API-økter og mellomlager. Standardroten ligger utenfor koden:
`~/Library/Application Support/familiekilder-mcp` på macOS,
`%LOCALAPPDATA%\familiekilder-mcp` på Windows og
`~/.local/share/familiekilder-mcp` (eller `$XDG_DATA_HOME/familiekilder-mcp`)
på Linux. En ny OS-bruker eller en ren installasjon på en annen maskin starter
uten dine data.

På samme maskin velges en separat profil med `--profile nabo`. Profilen ligger under `profiles/nabo` i dataroten.
Den arver aldri standardprofilens tjenester, passord eller økter. `--root` velger
en fullstendig datarot og kan ikke kombineres med `--profile`. Når en datarot
sendes videre internt, legges profilnavnet ikke til en gang til.

```sh
familiekilder-mcp --profile nabo auth spond configure --client-file /privat/mappe/klient.json
familiekilder-mcp --profile nabo configure spond
familiekilder-mcp --profile nabo password set spond
familiekilder-mcp --profile nabo auth spond browser-bootstrap
familiekilder-mcp --profile nabo mcp serve
```

Den andre profilen trenger sitt eget klientoppsett og sin egen innlogging.
`familiekilder-mcp --profile nabo setup` legger inn det innebygde oppsettet ved første innlogging
i veiviseren; se [klientoppsett](klientoppsett.md). Del en ren utsjekking eller
bygget pakke; ikke kopier `.private`, dataroten eller nettleserprofiler. Profiler i samme OS-konto beskytter mot sammenblanding;
for adskilte personer er egne OS-kontoer eller maskiner også nødvendig for å
ha separate filrettigheter og nøkkelringer.

## Passordhjelperen

Passord lagres bare i systemets egen nøkkelring, gjennom en
[Keyring-backend](https://keyring.readthedocs.io/en/latest/) som programmet
velger selv, aldri keyring-innstillinger eller miljøvariabler:

| System | Nøkkelring | `storage` i `password status` |
|---|---|---|
| macOS | macOS-nøkkelringen | `macos-keychain` |
| Windows | Legitimasjonsbehandling (Credential Manager) | `windows-credential-manager` |
| Linux | Secret Service, for eksempel GNOME Keyring eller KWallet | `secret-service` |

Det finnes ingen filbasert reserve. Finnes ingen nøkkelring, for eksempel på
Linux uten Secret Service, er passordhjelperen av. Du skriver da passordet selv
i nettleservinduet, og innloggingen virker ellers som vanlig.

Nøkkelringoppføringen er bundet til kanonisk datarot og passordtype.
`.private/login-passwords.json` inneholder bare referanser, aldri passord. En
kopiert referanse kan ikke brukes fra en annen datarot. Tokenfiler ligger i
den private lagringen, ikke i nøkkelringen: med 0700/0600-rettigheter på macOS
og Linux, og med en tilgangsliste bare for brukeren din på Windows.

```sh
familiekilder-mcp password set bankid
familiekilder-mcp password status bankid
familiekilder-mcp password delete bankid
```

`set` leser passordet to ganger med skjult terminalinntasting. Det finnes ikke et
passordargument eller en kommando som skriver passordet til stdout.
`delete` fjerner passordet, men logger ikke ut eksisterende API-økter.

| Tjeneste | Administrert innlogging | Passordoppføring |
| --- | --- | --- |
| Vigilo | `auth vigilo browser-login` | `bankid` |
| Skolemelding | `auth skolemelding browser-login` | `bankid` |
| IST Home | `auth ist-home browser-login` | `bankid` |
| Spond | `auth spond browser-bootstrap` | `spond` |

Telefonbekreftelse og engangskoder er manuelle. Hjelperen venter på et synlig,
tomt passordfelt på en tillatt HTTPS-origin, fyller fra gjeldende profil og
trykker en entydig Fortsett/Neste/Logg inn-knapp dersom den finnes. BankID-originene
er eksplisitte: `csfe.bankid.no`, `auth.bankid.no` og `login.bankid.no`.
BankID beskriver sine produksjonsendepunkter i
[tjenestedokumentasjonen](https://developer.bankid.no/bankid-esign-provider/service-endpoints/).
Ingen wildcard-domener brukes. Spond mottar aldri BankID-passordet. Hoopit
bruker et personlig tilgangstoken og har ingen passordoppføring.

Hjelperen gjør maksimalt ett utfyllingsforsøk per innlogging, også ved ukjent
resultat. Felt for passordendring, flere passordfelt og gjenkjent signering
avvises. Ukjent sidelayout, utilgjengelig nøkkelring eller utilgjengelig CDP gir
manuell videreføring. Brukernavn/fødselsnummer og valg av innloggingsmetode fylles
ikke av denne hjelperen. De eldre `login`-/`ist-auth login`-kommandoene med manuelt
kopiert callback og `import-session` har ingen styrt nettleser og bruker ikke
automatisk passordutfylling.

## Verifisert og gjenstående

Enhetstester dekker profilskille, manglende passord, kopierte referanser,
domeneavgrensning og at usikre forsøk ikke gjentas. Nøkkelringlagring på macOS
er prøvd med et midlertidig testpassord. Legitimasjonsbehandling på Windows og
Secret Service på Linux er nytt og bare dekket av enhetstester; det er ikke
prøvd med ekte innlogging ennå.

Testene er ikke en full produksjonsinnlogging. Passordutfylling og videreføring
må fortsatt bekreftes mot hver leverandørs faktiske skjermbilde og den manuelle
telefonbekreftelsen. Tester henter aldri det personlige BankID-passordet.
