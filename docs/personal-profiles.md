# Personlige profiler og passord

Koden er felles. Hver bruker har sin egen datarot med tjenestekonfigurasjon,
API-økter og mellomlager. Standardroten ligger utenfor koden:
`~/Library/Application Support/familiekilder-mcp` for kommandolinjen. En ny OS-bruker eller en
ren installasjon på en annen maskin starter uten dine data.

På samme maskin velges en separat profil med `--profile nabo`. Profilen ligger under `profiles/nabo` i dataroten.
Den arver aldri standardprofilens tjenester, passord eller økter. `--root` velger
en fullstendig datarot og kan ikke kombineres med `--profile`. Når en datarot
sendes videre internt, legges profilnavnet ikke til en gang til.

```sh
familiekilder-mcp --profile nabo onboard --bundle /privat/mappe/pakke.json
familiekilder-mcp --profile nabo configure spond
familiekilder-mcp --profile nabo password set spond
familiekilder-mcp --profile nabo auth spond browser-bootstrap
familiekilder-mcp --profile nabo mcp serve
```

Naboen trenger samme kode og sitt eget oppsett. Del en ren utsjekking eller
bygget pakke; ikke kopier `.private`, Application Support-katalogen, nettleser-
eller emulatorprofiler. Profiler i samme OS-konto beskytter mot sammenblanding;
for adskilte personer er egne OS-kontoer eller maskiner også nødvendig for å
ha separate filrettigheter og nøkkelringer.

## Passordhjelperen

Passord lagres i macOS-nøkkelringen gjennom den eksplisitte
[Keyring macOS-backenden](https://keyring.readthedocs.io/en/latest/).
Det finnes ingen filbasert reserve. Nøkkelringoppføringen er bundet til kanonisk
datarot og passordtype. `.private/login-passwords.json` inneholder bare referanser,
aldri passord. En kopiert referanse kan ikke brukes fra en annen datarot.
Tokenfiler ligger i den private lagringen med 0700/0600-rettigheter, ikke i
nøkkelringen.

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
| Vigilo | `auth vigilo browser-login` eller `android-login` | `bankid` |
| Skolemelding | `auth skolemelding browser-login` eller `android-login` | `bankid` |
| IST Home | `auth ist-home browser-login` | `bankid` |
| Spond | `auth spond browser-bootstrap` | `spond` |
| Hoopit | `auth hoopit browser-bootstrap` | `hoopit` |

Telefonbekreftelse og engangskoder er manuelle. Hjelperen venter på et synlig,
tomt passordfelt på en tillatt HTTPS-origin, fyller fra gjeldende profil og
trykker en entydig Fortsett/Neste/Logg inn-knapp dersom den finnes. BankID-originene
er eksplisitte: `csfe.bankid.no`, `auth.bankid.no` og `login.bankid.no`.
BankID beskriver sine produksjonsendepunkter i
[tjenestedokumentasjonen](https://developer.bankid.no/bankid-esign-provider/service-endpoints/).
Ingen wildcard-domener brukes. Spond/Hoopit mottar aldri BankID-passordet.

Hjelperen gjør maksimalt ett utfyllingsforsøk per innlogging, også ved ukjent
resultat. Felt for passordendring, flere passordfelt og gjenkjent signering
avvises. Ukjent sidelayout, utilgjengelig nøkkelring eller utilgjengelig CDP gir
manuell videreføring. Brukernavn/fødselsnummer og valg av innloggingsmetode fylles
ikke av denne hjelperen. De eldre `login`-/`ist-auth login`-kommandoene med manuelt
kopiert callback og `import-session` har ingen styrt nettleser og bruker ikke
automatisk passordutfylling.

## Android

Android Chrome må kjøre med ADB-tilgjengelig DevTools. Hjelperen åpner en ny
innloggingsfane, sender passordet over en midlertidig lokal ADB/CDP-forbindelse og
lukker fanen og forbindelsen etterpå. Passordet sendes aldri som et ADB-argument.
Chrome på den testede emulatoren støtter ikke `Target.createBrowserContext`.
Derfor må enheten bindes eksplisitt til én personlig profil før noen Android-innlogging starter.
Bruk `--profile NAVN android-device bind --adb-serial ENHET` på din egen enhet;
ikke bind en annen persons eksisterende nettleser. En ubundet enhet avvises også.
En annen profil avvises og må bruke en annen enhet/emulator. Koblingen ligger
privat per OS-bruker i `.private/android-device-profiles.json` under standardroten,
uavhengig av overstyrt datarot. Ingen automatisk overtakelse av en brukt enhet.

## Verifisert og gjenstående

Enhetstester dekker profilskille, manglende passord, kopierte referanser,
Android-eierskap, domeneavgrensning og at usikre forsøk ikke gjentas. Nativ
nøkkelringlagring er prøvd med et midlertidig testpassord. DOM-hjelperen er prøvd
mot kontrollerte, nettverksfrie testsider i ekte Android Chrome:

```sh
FAMILIEKILDER_ANDROID_TEST_SERIAL=emulator-5554 scripts/test tests/test_password_dom.py
```

Dette er ikke en full produksjonsinnlogging. Passordutfylling og videreføring
må fortsatt bekreftes mot hver leverandørs faktiske skjermbilde og den manuelle
telefonbekreftelsen. Tester henter aldri det personlige BankID-passordet.

Android-returhjelperens Java-kilde og manifest følger med både kildepakken og
Python-wheel. Bygging av den lokale APK-en krever fortsatt Android SDK og Java;
ingen ferdig signert APK eller nøkkel deles. Signeringsnøkkel og APK bygges i
den valgte profilens private katalog.
