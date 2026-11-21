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

## Hoopit: token i stedet for oppsett

Hoopit har ikke klientoppsett. Du kobler til med et personlig tilgangstoken som
du lager hos Hoopit; se [Hoopit](hoopit-ingestion.md).

## Tjenester med innebygd oppsett

For Vigilo, Skolemelding, Spond og IST Home følger appidentiteten med
pakken. Velg «Logg inn» i veiviseren (`familiekilder-mcp setup`). Mangler
tjenesten klientoppsett, legger veiviseren inn det innebygde oppsettet og åpner
innloggingen. Du trenger ingen fil.

En tjeneste uten innebygd oppsett vises som «Ikke tilgjengelig i veiviseren
ennå». Den kan fortsatt brukes med
[import fra kommandolinjen](#importere-oppsett-fra-kommandolinjen).

**Flyt:** Veiviseren spør om kommune i innloggingsdialogen. Kommunelisten er
Flyts åpne liste og krever ikke innlogging. Valget lagres som klientoppsett for
Flyt. Dialogen viser den lagrede kommunen, og du kan velge en annen før du
logger inn. Det nye valget erstatter det lagrede. Har Flyt en lagret
innlogging, bytter du kommune med «Logg inn på nytt» og krysser av for «Erstatt
den eksisterende innloggingen». Da logges du ut av Flyt med en gang, også om du
avbryter den nye innloggingen. Finnes ikke den nye kommunen i listen, endres
ingenting. Se [Flyt](flyt.md).

**Gmail:** Du trenger din egen Google OAuth-klient av typen Desktop app. Prosjektet
deler ikke ut en felles Google-klient. Opprett klienten i Google Cloud Console og
velg filen med «Velg Google-fil» i veiviseren. Se
[Gmail-veiledningen](gmail.md#oppsett). Valgte du feil fil, velger du
«Velg en annen Google-fil» på Gmail-raden før du logger inn. Den nye filen
erstatter den lagrede bare hvis den er en gyldig Desktop app-klient; ellers
beholdes den gamle. Med en lagret Gmail-innlogging byttes ikke filen; logg ut
med `familiekilder-mcp auth gmail logout` først.

## Nytt innebygd oppsett etter en pakkeoppdatering

Det innebygde oppsettet hører til en bestemt versjon av leverandørens app.
Noen leverandører krever etter hvert en nyere app. Da kommer det et nytt
innebygd oppsett i en ny versjon av pakken.

Et oppsett som veiviseren selv har lagt inn fra det innebygde, byttes ut av seg
selv når pakken har et nyere. Det skjer når veiviseren åpnes
(`familiekilder-mcp setup`) og når MCP-serveren starter
(`familiekilder-mcp mcp serve`, som AI-appen starter). Etter en
pakkeoppdatering holder det altså å starte AI-appen på nytt. Byttet bruker ikke
nettet.

For å kjenne igjen sine egne oppsett lagrer veiviseren et merke ved siden av
oppsettet under `.private/credentials/` i dataroten, for eksempel
`vigilo-default-setup.json`. Merket inneholder ingen verdier, bare to
SHA-256-fingeravtrykk: ett av den lagrede oppsettsfilen og ett av det innebygde
oppsettet den kom fra. Merket gjelder bare så lenge filen er nøyaktig slik
veiviseren skrev den.

Disse oppsettene byttes aldri ut automatisk:

- Et oppsett veiviseren la inn i 1.0.0rc1 eller 1.0.0rc2. Det har ikke noe
  merke og behandles som ditt eget. I praksis gjelder det Spond, som veiviseren
  la inn allerede i 1.0.0rc1 og 1.0.0rc2. Følg stegene for eget oppsett under
  hvis du vil gå over til et oppsett som fornyes.
- Et oppsett du har importert selv med `auth … configure`, også om det skjedde
  etter at veiviseren la inn det innebygde.
- Et oppsett du har endret i filen.
- Et oppsett som ikke kan leses.
- Oppsettet for Flyt og Gmail. Det har ikke noe innebygd oppsett.
- Oppsett i en datarot med en signert providerpakke (`onboard --bundle`).

Innloggingen din slettes aldri av byttet. Gjelder det nye oppsettet bare en
nyere appversjon, beholdes innloggingen. Endrer det appidentiteten (klient-ID,
klienthemmelighet eller adresser), avviser Vigilo, Skolemelding og IST Home
byttet så lenge tjenesten har en lagret innlogging. Da beholdes både det
gamle oppsettet og innloggingen, og byttet prøves igjen ved neste start. Slik
tar du det nye oppsettet i bruk:

- Velg «Logg inn på nytt» i veiviseren og kryss av for «Erstatt den
  eksisterende innloggingen». Dialogen sier fra om at et nytt innebygd
  oppsett tas i bruk. Veiviseren logger deg da ut av tjenesten med en gang,
  legger inn det nye oppsettet og åpner innloggingen. Avbryter du innloggingen,
  er du logget ut.
- Eller logg ut med for eksempel `familiekilder-mcp auth vigilo logout`, og
  åpne veiviseren eller start AI-appen på nytt.

Har du importert et eget oppsett og vil gå over til det innebygde:

1. Logg ut av tjenesten, for eksempel `familiekilder-mcp auth vigilo logout`.
2. Slett det lagrede oppsettet under `.private/credentials/` i dataroten:
   `vigilo-client.json`, `skolemelding-client.json`, `spond-provider.json`
   eller `ist-home-client.json`.
3. Velg «Logg inn» i veiviseren. Den legger da inn det innebygde oppsettet og
   merker det, så senere versjoner fornyer det.

Bytter en leverandør verdiene eller sperrer appidentiteten, slutter koblingen å
virke for alle som bruker den. Standardverdien fjernes da fra pakken, og
tjenesten vises som «Ikke tilgjengelig i veiviseren ennå» til et nytt oppsett er
kontrollert. Et lagret oppsett blir stående som det er. I mellomtiden kan du
importere et gyldig oppsett fra kommandolinjen.

## Importere oppsett fra kommandolinjen

Har du et eget klientoppsett, kan du importere det fra kommandolinjen. Et
importert oppsett erstatter det som er lagret, også det innebygde. Veiviseren
bruker deretter det du importerte, og en nyere pakke bytter det ikke ut. Logg
ut før importen hvis tjenesten har en lagret innlogging; noen koblinger avviser
ellers et oppsett med en annen appidentitet.

Innlogging som bare gjøres fra kommandolinjen, legger ikke inn det innebygde
oppsettet. Bruk da veiviseren for første innlogging, eller importer en fil.

Bytt `vigilo` med kilde-ID-en fra tabellen under:

```sh
familiekilder-mcp auth vigilo configure --client-file /privat/mappe/klient.json
familiekilder-mcp configure vigilo
familiekilder-mcp auth vigilo browser-login
```

Spond bruker `browser-bootstrap` i stedet for `browser-login`. Gmail bruker
`auth gmail login`; se [Gmail](gmail.md#oppsett).

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
| IST Home (`ist-home`) | `authority`, `client_id`, `client_secret`, `redirect_uri`, `scope` |
| Flyt (`flyt`) | `municipality` (kommunens navn slik det står i Flyts åpne kommuneliste, eller organisasjonsnummeret), eller én oppføring fra listen: `organizationNumber`, `customerName`, `displayName`, `vismaConnectClientContext` |
| Gmail (`gmail`) | Google-filen for en egen OAuth-klient av typen Desktop app; se [Gmail](gmail.md#oppsett). |

## Egen vert som prøveordning

Lokal stdio er standard. For HTTP, OAuth, Fly.io og flytting av innlogginger,
se [alltid-på-oppsettet](alltid-pa.md). Innloggingen gjøres fortsatt på
datamaskinen din; ikke kopier øktfiler for hånd.
