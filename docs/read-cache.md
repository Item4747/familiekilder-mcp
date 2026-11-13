# Mellomlagring og trafikkbudsjett

MCP-serveren leser tjenestene når et verktøy kalles, og har ingen egen
innhenting i bakgrunnen. Det som faktisk er hentet, mellomlagres kort og privat,
så samme oppslag ikke sendes til tjenesten mange ganger på rad.

## Kildeinnhold og vedlegg

Meldinger, tråder, tekstuttrekk og renderte sider beholdes i høyst sju dager fra
hentingen. Lokal lesing forlenger ikke levetiden. Nye kontroller kan erstatte
kopien. Det hentes aldri ekstra historikk for å fylle mellomlageret.

| Oppslag | Standard | Ny kontroll |
| --- | --- | --- |
| `*_get_item` | Gjenbruk i opptil fem minutter | `freshness="fresh"` |
| Tilbakeblikk | `freshness="cached"` bruker beholdt kopi i opptil sju dager | Cache-miss henter kilden; ingen offline-garanti |
| `*_list_items` | Gjenbruk i opptil 60 sekunder; tekstfiltre deler innhentingen | `refresh=true` uten cursor |
| `gmail_search` | Identisk query, API-side og filtre gjenbrukes i opptil 60 sekunder | Etter 60 sekunder hentes siden igjen |
| `*_read_attachment` | Beholdt tekst og renderte sider for samme observerte kildeversjon | `refresh=true` uten preparation_token |

Vedleggsverktøyet støtter også `freshness="cached"` for å gå tilbake til beholdt
melding og vedlegg uten ny kildetrafikk ved cachetreff. Mangler filkopien,
kontrolleres kilden før en ny fil hentes; en endret kilde avvises som tilbakeblikk.

## Ferskhet og handlinger

Svar oppgir `cache.hit`, `age_seconds`, `fetched_at` og `expires_at`.
Hentetid er opprinnelig kildehenting. En cachelesing gir aldri en ny
ferskhetskvittering, og en observasjon er gyldig i 15 minutter også ved gjenbruk.
Intern bibliotekskode for skrivehandlinger leser direkte fra tjenesten før og
etter sending, med egne rettighets- og etterkontroller. Den selvstendige
serveren tilbyr bare lesing. Cacheinnhold gir ingen skriverettigheter.

Vedlegg knyttes til konto, referanse, kildeinnholdets fingeravtrykk og vedleggs-ID.
Ved ny nedlasting sammenlignes den ferskt oppløste meldingen med denne
observasjonen før filen lastes ned. Endret kilde avvises uten vedleggscache eller
lesekvittering; hent og vurder meldingen på nytt. For andre vedlegg enn PDF
lagres bare ferdige tekst- og bildesider, og originalfilen fjernes etter lesing
eller feil. For PDF lagrer den trinnvise leseren originalen og dens SHA-256; se
[trinnvis PDF-lesing](pdf-window-reading.md).
Endret kildeversjon gir en annen cacheoppføring. Leste sider kvitteres bare i den
aktuelle MCP-prosessens matchende observasjon; gamle lesekvitteringer overføres
ikke. Dersom leverandøren erstatter filbytes uten synlig metadataendring, kan
metadata ikke bevise endringen. Bruk `refresh=true` for ny nedlasting.

Tekstvedlegg (TXT/HTML, GZIP, meldingsheader og ZIP-tekst) deles i sider med
unike sidenumre og høyst 16 000 tegn per side. Samlet grense er 100 sider og
2 MiB UTF-8-tekst, inkludert ZIP-etiketter. Overskridelse avvises uten
lesekvittering eller vellykket vedleggscache. Alle sider må leses før vedlegget
kvitteres. Renderingsversjonen inngår i cachenøkkelen, så oppføringer fra en
eldre versjon gjenbrukes ikke.

## Felles snapshot for en oppgave

En vert som bygger på serveren, kan la flere verktøykall dele ett snapshot av
kildene (`sync_snapshot.SyncSnapshot`, `Runtime.snapshot_key()`). Så lenge
nøkkelen er den samme, gjenbrukes hvert konkrete oppslag uten tidsbasert utløp:
profiler/grupper, meldinger, arrangementer, detaljer, søkesider og
versjonsbundne vedlegg, også lesende POST-søk og GraphQL-queryer. Innlogging,
tokenkall og skrivehandlinger går aldri gjennom snapshotet. Standardserveren
(`familiekilder-mcp mcp serve`) har ingen slik nøkkel.

Snapshotet fylles ved behov, uten forhåndsinnhenting. Et sammendrag erstatter
ikke uleste detaljer, og det er ikke en atomisk kopi av alle tjenestene på ett
klokkeslett. Opprinnelig hentetid og faktisk alder beholdes. Nøkkelen inkluderer
kilde, konto, klient/providerkonfigurasjon og oppslagsparametrene. Kontobytte,
manglende innlogging, providerendring og kildefeil ugyldiggjør berørt kildes
snapshot. Det har samme størrelsesgrenser som mellomlageret under, og
`family_connections.sync_snapshot` viser innholdsfrie tellere per kilde.

## Lagring og sletting

- `.private/read-cache` i dataroten (per profil): katalogmodus 0700, filer 0600.
- Høyst 256 MiB og 2 000 oppføringer totalt; høyst 64 MiB per oppføring.
  Grensen måler lagret JSON inkludert base64. Større svar leses uten caching.
  Eldste oppføringer slettes først.
- Opprydding ved oppstart, ved lesing og hvert minutt mens MCP kjører. Når MCP
  er avsluttet, fjernes utløpte filer ved neste oppstart.
- Konto-/klientbinding kontrolleres lokalt før gjenbruk. Kontobytte, manglende
  økt eller deaktivert tjeneste fjerner kopiene ved opprydding. Sidereferanser,
  vedleggstokens og observasjoner avvises etter kontobytte.
- Token og private nedlastingsspesifikasjoner lagres ikke i det persistente
  mellomlageret. Ufullstendige kildeoppslag og mislykket nedlasting/rendring
  caches ikke som vellykket innhold. Kildefeil erstattes ikke skjult av en gammel
  kopi.

## Samtidighet og trafikk

Prosesser deler kilde-/kontolåsen under et cache-miss. Ventende lesere gjenbruker
det ferdige resultatet, også ved samtidige ferske oppslag. Ingen cache deles
mellom profiler.

Eksisterende pacing, pauser og Retry-After beholdes. I tillegg reserveres trafikk
varig på tvers av prosesser: høyst 1 000 forespørsler per timevindu og 3 000 per
døgnvindu per installasjon og tjeneste. Kontobytte nullstiller ikke budsjettet.
Nye forsøk telles, cachetreff sender ingen forespørsel. Et oppbrukt budsjett
stopper før sending og oppgir neste mulige forsøk. Grensene er lokale vern, ikke
leverandørgodkjente kvoter eller garanti mot utestenging. Separate
tjenestegrenser gjelder fortsatt.

Innholdsfrie treff-/loadertellere finnes i `family_connections.read_cache`.
Transportreservasjoner finnes i `credentials/*-request-budget.json`.
Loader-kall og HTTP-forespørsler er ulike tellere.
