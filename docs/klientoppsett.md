# Klientoppsett

Hver tjeneste trenger to ting: brukerkontoen din og et oppsett for selve
koblingen. Oppsettet kalles klientoppsett eller klientkonfigurasjon. Det
beskriver hvilken app koblingen opptrer som hos leverandøren: appens
identitet (klient-ID, for noen tjenester en klienthemmelighet), adressen
innloggingen sendes tilbake til, og hvilke verter API-et og vedleggene ligger på.

## Hva appidentiteten er, og hva den ikke er

Appidentiteten er den samme for alle som bruker leverandørens offisielle app.
Verdiene er hentet fra leverandørens offisielle apppakke eller nettside, ikke fra
en bestemt installasjon eller bruker.

- Den er ikke en innlogging. Den gir ikke tilgang til noen konto, heller ikke
  din. Du må fortsatt logge inn selv, med BankID eller passord, i
  nettleservinduet som åpnes.
- En klienthemmelighet som ligger i en app alle kan laste ned, regnes ikke som
  hemmelig. Den kan ikke bevise hvilken app som spør; se
  [RFC 8252, punkt 8.5](https://www.rfc-editor.org/rfc/rfc8252#section-8.5).
- Innloggingen din, tokens og økter lagres bare privat på maskinen din. De
  følger aldri med pakken og legges aldri i Git.

Koblingene er uoffisielle. Leverandørene har ikke godkjent dem.

## Tjenester med innebygd oppsett

For Vigilo, Skolemelding, Spond, Hoopit og IST Home følger appidentiteten med
pakken. Velg «Logg inn» i veiviseren (`familiekilder-mcp setup`). Mangler
tjenesten klientoppsett, legger veiviseren inn det innebygde oppsettet og åpner
innloggingen. Du trenger ingen fil.

En tjeneste uten innebygd oppsett vises som «Ikke tilgjengelig i veiviseren
ennå». Den kan fortsatt brukes med
[import fra kommandolinjen](#importere-oppsett-fra-kommandolinjen).

**Flyt:** Veiviseren spør om kommune i innloggingsdialogen. Kommunelisten er
Flyts åpne liste og krever ikke innlogging. Valget lagres som klientoppsett for
Flyt. Så lenge Flyt ikke har en lagret innlogging, viser dialogen den lagrede
kommunen, og du kan velge en annen før du logger inn. Det nye valget erstatter
det lagrede. Med en lagret innlogging bytter ikke veiviseren kommune; logg ut
med `familiekilder-mcp auth flyt logout` først. Se [Flyt](flyt.md).

**Gmail:** Du trenger din egen Google OAuth-klient av typen Desktop app. Prosjektet
deler ikke ut en felles Google-klient. Opprett klienten i Google Cloud Console og
velg filen med «Velg Google-fil» i veiviseren. Se
[Gmail-veiledningen](gmail.md#oppsett). Valgte du feil fil, velger du
«Velg en annen Google-fil» på Gmail-raden før du logger inn. Den nye filen
erstatter den lagrede bare hvis den er en gyldig Desktop app-klient; ellers
beholdes den gamle. Med en lagret Gmail-innlogging byttes ikke filen; logg ut
med `familiekilder-mcp auth gmail logout` først.

## Et lagret oppsett byttes ikke automatisk

Veiviseren bruker det innebygde oppsettet bare når tjenesten ikke har noe
klientoppsett fra før. Et oppsett som allerede er lagret, blir aldri byttet ut
av veiviseren, heller ikke når pakken oppdateres med nye standardverdier.
Unntakene er når du selv velger en annen kommune for Flyt eller en annen
Google-fil for Gmail, og tjenesten ikke har en lagret innlogging.

Det innebygde oppsettet hører til en bestemt versjon av leverandørens app.
Noen leverandører krever etter hvert en nyere app. Da slutter innloggingen å
virke, og standardverdien må fornyes i en ny versjon av pakken.

Det finnes ingen kommando som bytter et lagret oppsett med en ny standardverdi.
Slik tar du den i bruk etter at pakken er oppdatert:

1. Logg ut av tjenesten, for eksempel `familiekilder-mcp auth vigilo logout`.
2. Slett det lagrede oppsettet under `.private/credentials/` i dataroten:
   `vigilo-client.json`, `skolemelding-client.json`, `spond-provider.json`,
   `hoopit-client.json` eller `ist-home-client.json`.
3. Velg «Logg inn» i veiviseren. Den legger da inn det nye innebygde oppsettet.

Bytter en leverandør verdiene eller sperrer appidentiteten, slutter koblingen å
virke for alle som bruker den. Standardverdien fjernes da fra pakken, og
tjenesten vises som «Ikke tilgjengelig i veiviseren ennå» til et nytt oppsett er
kontrollert. I mellomtiden kan du importere et gyldig oppsett fra
kommandolinjen.

## Importere oppsett fra kommandolinjen

Har du et eget klientoppsett, kan du importere det fra kommandolinjen. Et
importert oppsett erstatter det som er lagret, også det innebygde. Veiviseren
bruker deretter det du importerte. Logg ut før importen hvis tjenesten har en
lagret innlogging; noen koblinger avviser ellers et oppsett med en annen
appidentitet.

Innlogging som bare gjøres fra kommandolinjen, legger ikke inn det innebygde
oppsettet. Bruk da veiviseren for første innlogging, eller importer en fil.

Bytt `vigilo` med kilde-ID-en fra tabellen under:

```sh
familiekilder-mcp auth vigilo configure --client-file /privat/mappe/klient.json
familiekilder-mcp configure vigilo
familiekilder-mcp auth vigilo browser-login
```

Spond og Hoopit bruker `browser-bootstrap` i stedet for `browser-login`. Gmail
bruker `auth gmail login`; se [Gmail](gmail.md#oppsett).

Klientfilen må eies av din OS-bruker, være en vanlig fil og ha filmodus `0600`.
Bruk en privat mappe med modus `0700`. Ikke legg filen i Git og ikke send den til
andre.

**IST Home fra Mac-appen:** Har du IST Home-appen installert på Macen og er
logget inn i den, kan kommandoen under hente klientoppsettet fra appens lokale
data. Eksporten gir ikke tilgang til kontoen din:

```sh
umask 077
mkdir -p .private
familiekilder-mcp ist-auth export-mac-client --output "$PWD/.private/ist-home-client.json"
familiekilder-mcp auth ist-home configure --client-file "$PWD/.private/ist-home-client.json"
```

Støtter eksporten ikke den installerte appen, meld versjonen som et spørsmål i
prosjektet. Ikke del eksportfilen i en feilrapport.

Den utgitte pakken inneholder ingen betrodde nøkler for signerte
providerpakker. Bruk det innebygde oppsettet eller en egen klientfil.

## Felter i klientoppsettet

Tabellen er hentet fra `sources.REGISTRY` og koblingenes konfigurasjonsklasser.
Noen felt har standardverdier eller er valgfrie. Importen kontrollerer også
at endepunkter, appidentitet og andre verdier passer til tjenesten. Tabellen
er en formatoversikt, ikke en fil med verdier som kan brukes til innlogging.

| Tjeneste og kilde-ID | Felter |
|---|---|
| Vigilo (`vigilo`) | `authority`, `client_id`, `client_secret`, `redirect_uri`, `scope`, `api_root`, `app_version`, `attachment_hosts` |
| Skolemelding (`skolemelding`) | `oidc_client_id`, `portal_client_id`, `app_version`, `issuer`, `redirect_uri`, `scopes`, `authorization_endpoint`, `oidc_token_endpoint`, `attachment_hosts`, `attachment_path_prefixes` |
| Spond (`spond`) | `api_root`, `app_version`, `attachment_hosts` |
| Hoopit (`hoopit`) | `api_root`, `firebase_api_key`, `firebase_project_id`, `app_version`, `attachment_hosts` |
| IST Home (`ist-home`) | `authority`, `client_id`, `client_secret`, `redirect_uri`, `scope` |
| Flyt (`flyt`) | `municipality` (kommunenavn eller organisasjonsnummer, slås opp i Flyts åpne kommuneliste), eller én oppføring fra listen: `organizationNumber`, `customerName`, `displayName`, `vismaConnectClientContext` |
| Gmail (`gmail`) | Google-filen for en egen OAuth-klient av typen Desktop app; se [Gmail](gmail.md#oppsett). |
