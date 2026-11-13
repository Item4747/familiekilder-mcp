# Klientoppsett

Hver tjeneste trenger både brukerkontoen din og oppsettet for selve koblingen.
Dette oppsettet kalles klientkonfigurasjon. Det er ikke det samme som passordet
ditt. Spond klargjøres automatisk; for andre tjenester må oppsettet skaffes
eller hentes fra din egen klient.

## Oppsett du kan gjøre med verktøyene som følger med

**Spond:** Velg «Logg inn» i veiviseren. Den lager nettklientoppsettet lokalt
fra de offentlige API- og filvertinnstillingene i koblingen. Det trengs ingen
klienthemmelighet eller oppsettsfil. En eksisterende klientkonfigurasjon
overskrives ikke. Du logger deretter inn selv i det isolerte Brave-vinduet.

**Gmail:** Opprett din egen Google OAuth-klient av typen Desktop app og følg
[Gmail-veiledningen](gmail.md#oppsett). Du trenger tilgang til Google Cloud
Console. Prosjektet deler ikke ut en felles Google-klient.

**IST Home:** Hvis du har IST Home-appen installert på Macen og har logget inn i den,
kan kommandoen under hente klientoppsettet fra de lokale appdataene. Kjør fra
prosjektmappen etter installasjon:

```sh
umask 077
mkdir -p .private
familiekilder-mcp ist-auth export-mac-client --output "$PWD/.private/ist-home-client.json"
familiekilder-mcp auth ist-home configure --client-file "$PWD/.private/ist-home-client.json"
familiekilder-mcp configure ist-home
familiekilder-mcp auth ist-home browser-login
```

Eksporten gir ikke tilgang til kontoen din. Du logger inn selv i nettleseren.
Hvis eksporten ikke støtter den installerte appen, stopp og meld versjonen som
et spørsmål i prosjektet. Ikke del eksportfilen i en feilrapport.

## Øvrige koblinger

Koblingene kan importere en privat klientfil med oppsett fra tjenestens egen
klient. Det følger foreløpig ikke med en komplett oppskrift som skaffer denne
filen for alle tjenestene. De kan derfor ikke presenteres som ferdig oppsett
for en ny bruker uten teknisk hjelp. Dette må løses eller avgrenses før stabil
1.0; se [lanseringsplanen](release.md).

Hvis du allerede har en gyldig fil, importer den slik. Bytt `spond` med
kilde-ID-en fra tabellen:

```sh
familiekilder-mcp auth spond configure --client-file /privat/mappe/klient.json
familiekilder-mcp configure spond
familiekilder-mcp auth spond browser-bootstrap
```

Klientfilen må eies av din OS-bruker, være en vanlig fil og ha filmodus `0600`.
Bruk en privat mappe med modus `0700`. Ikke legg filen i Git eller send den til
andre. Spond og Hoopit bruker `browser-bootstrap`; de andre nettleserkoblingene
bruker `browser-login`. Den utgitte pakken inneholder ingen betrodde nøkler for
signerte providerpakker; bruk derfor separate klientfiler i standardoppsettet.

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
| Gmail (`gmail`) | Google-filen for en egen OAuth-klient av typen Desktop app; se [Gmail](gmail.md#oppsett). |
