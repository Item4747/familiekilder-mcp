# Gmail: målrettet lesing

Gmail er en egen lesekilde i MCP-serveren. `gmail_search`, `gmail_get_item`
og `gmail_read_attachment` bruker Googles offisielle API med bare
`gmail.readonly`. Sending, arkivering, etiketter, lest-status og sletting er ikke
implementert eller eksponert. Det finnes ingen `gmail_list_items`: Gmail har
ingen feed. Lesing skjer bare som svar på et søk; et bredt søk kan likevel
treffe mange eller alle meldingene.

## Målrettet søk

`gmail_search(query, limit=20, cursor=None, include_spam_trash=false)` sender
`query` uendret til Googles `messages.list`-API. Bruk dette når agenten vil lete
etter bestemte e-poster, for eksempel etter å ha funnet en aktivitet i Spond.
Søket har ingen automatisk datogrense eller relevansfiltrering.

Eksempler:

```json
{"query": "from:klubben.no subject:påmelding after:2026/08/01", "limit": 20}
{"query": "{parkour turn} has:attachment", "limit": 10}
```

[Gmail API-søkesyntaks](https://developers.google.com/workspace/gmail/api/guides/filtering)
omfatter avsender, emne, etiketter, datoer, vedlegg, OR og `{…}`. API-et søker
per melding og har ikke Gmail-nettsidens automatiske aliasutvidelse eller søk
over hele tråden. Datoer som `2026/08/01` tolkes i PST; bruk epoch-sekunder i
`after:`/`before:` for presise grenser i andre tidssoner. Søket legger ikke til
filtre for utkast eller chats. Spam og papirkurv utelates som standard; sett
`include_spam_trash=true` for å inkludere dem i søkeområdet.

Hvert kall henter én API-side med `limit` 1–50 treff, og fulltekst bare for disse
treffene. Følg `next_cursor` med samme `query` og `include_spam_trash`; `limit`
kan endres. Markøren oppbevares bare i MCP-prosessens minne og utløper etter
15 minutter. Flere sider krever flere agentkall. `complete=false` mens flere
sider eller begrensninger gjenstår. `complete=true` på siste side betyr at alle
sidene er lest uten rapporterte mangler. Identiske API-sider gjenbrukes i opptil
60 sekunder med opprinnelig hentetid; se [mellomlagring](read-cache.md). Dette er
ikke et fast øyeblikksbilde av postkassen; den kan endres mellom sidene.

En vert som bygger på serveren, kan sette et tak på antall treff per oppgave.
Ved taket returneres delresultater med `complete=false`,
`incomplete_reason=read_budget` og `restart_required=false`, uten markør som
hopper over uleste ID-er.

Kvotefeil/kildefeil etter minst ett lest treff bevarer treffene med
`complete=false`, `restart_required=true` og uten `next_cursor`, så uleste ID-er
ikke hoppes over. Start da søket på nytt når trafikken igjen er tilgjengelig.
Feil før første treff og manglende innlogging er feil, ikke tomme søkeresultater.
Slettede meldinger og gjentatte ID-er gir ufullstendig dekning; gjentatte
sidemarkører stopper videre paginering. Begrensninger følger sidemarkøren til
siste side. Manglende treff betyr aldri avlysning.

Treffene har samme referanser, observasjonstoken og vedleggsmetadata som andre
leseverktøy. En fullstendig lest melding kan følges opp selv om flere søkesider
gjenstår; meldingens egen `content_complete` kontrolleres separat. Bruk
`gmail_get_item` og `gmail_read_attachment` for ferske detaljer og vedlegg.
Søket deler eksisterende tokenfornyelse, leselås og kvotestyring. Normalisert
meldingstekst kan mellomlagres privat i høyst sju dager; søkestrengen lagres
ikke i mellomlageret. Ingen e-post endres.

MIME-tekst over 64 000 tegn markeres ufullstendig; vedlegg over 25 MiB avvises.
Det finnes ingen egen klassifikator: agenten du bruker, vurderer treffene.

## Tempo og kvotegjenoppretting

Alle Gmail-GET-kall bruker samme private trafikktilstand og kildelås, også på
tvers av lokale MCP-prosesser. Samtidige lesekall venter opptil 30 sekunder på
låsen. Et tidsavbrudd skjer før forespørsel eller trafikkreservasjon, og kan
forsøkes igjen etter at det pågående oppslaget er ferdig. Lange oppslag
eller innlogging kan fortsatt holde låsen utover ventetiden. Autentisering
og andre tjenester beholder eksisterende låsatferd.
Normaltempo er minst 0,5 sekunder mellom kall; dette er en lokal margin, ingen
påstand om Google-kvoten til din OAuth-klient.
`rateLimitExceeded`, `userRateLimitExceeded` og HTTP 429 gir opptil to nye
forsøk av den samme lesingen, med eksponentiell backoff fra ett sekund og jitter.
HTTP 500/502/503/504 bruker også avgrenset backoff. `Retry-After` respekteres
både som sekunder og HTTP-dato. Den lokale ventingen avgrenses til ti sekunder;
lengre pauser lagres og returneres som `retry_at` uten å vente gjennom hele pausen.

Ved hastighetsavvisning senkes tempoet automatisk, opp til fire sekunder mellom
kall; etter 20 vellykkede kall økes tempoet forsiktig igjen. Oppbrukte forsøk
gir minst ett minutts delt pause. `dailyLimitExceeded` gir minst én times pause
uten umiddelbar retry. 401 gir fortsatt bare én tokenfornyelse; andre 403-feil
behandles som tilgangsfeil. Ingen feiltekst fra Google lagres eller returneres.
Trafikkfilen inneholder bare faste årsakskoder, tellere og tidspunkter.

Slettede meldinger, uleselige treff og kvotepauser vises som begrensninger i
søkeresultatet. Vedleggsidentiteten er MIME-delens `partId` (strukturell sti som
reserve); Googles skiftende `attachmentId` er bare transporttoken og inngår ikke
i innholdshashen.

## Oppsett

Du trenger din egen Google OAuth-klient av typen **Desktop app**, med Gmail API
aktivert i Google Cloud-prosjektet ditt. Last ned klientfilen til en privat mappe
(filmodus 0600), for eksempel `.private` i dataroten, og kjør:

```sh
familiekilder-mcp configure gmail
familiekilder-mcp auth gmail configure --client-file "$HOME/Library/Application Support/familiekilder-mcp/.private/google-desktop.json"
familiekilder-mcp auth gmail login
familiekilder-mcp auth gmail status
```

Innlogging åpner systemnettleseren med PKCE og en midlertidig 127.0.0.1-retur.
Google ber bare om `gmail.readonly`. Ny tilgang verifiseres mot `/profile` før
en gammel økt erstattes. Access-token fornyes automatisk, og et 401-svar kan
føre til én fornyelse og ett nytt GET-forsøk. Grant og klientmetadata ligger
under `.private/credentials`; de skal ikke legges i Git.
`auth gmail logout` sletter bare det lokale grantet. Den tilbakekaller ikke
andre Google-tilkoblinger. Grantet kan også trekkes tilbake i Google-kontoen.

Skal andre enn deg selv bruke OAuth-klienten din, er Googles verifisering et eget
krav; at innloggingen virker lokalt, beviser ikke at klienten er godkjent.

## Kilder

- [Google: Gmail-søk](https://developers.google.com/workspace/gmail/api/guides/filtering),
  [Gmail-scopes](https://developers.google.com/workspace/gmail/api/auth/scopes) og
  [desktop OAuth/PKCE](https://developers.google.com/identity/protocols/oauth2/native-app).
- [Google: kvoter](https://developers.google.com/workspace/gmail/api/reference/quota) og
  [feilhåndtering/backoff](https://developers.google.com/workspace/gmail/api/guides/handle-errors).
