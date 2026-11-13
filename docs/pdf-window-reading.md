# Trinnvis PDF-lesing

PDF-er leses i avgrensede vinduer. Agenten får originaltekst og sidebilder side
for side og leser dem selv. Leseren er standard, med samme grenser for alle
profiler.

## Bryter

`FAMILIEKILDER_PDF_WINDOWS` har standardverdi `1`. `0` gir den eldre leseren og
er bare ment for tilbakeføring ved feil, ikke vanlig konfigurasjon. Andre verdier
avvises. En kjørende MCP-server beholder valgt leser til den startes på nytt.

## API

`*_read_attachment` har `after_page` og `after_chunk` (heltall fra 0). Start med
`after_page=0, after_chunk=0`. Fortsett med **begge** `next_after_page` og
`next_after_chunk`, og samme `preparation_token`.

- `after_page` er antallet fysiske sider før ønsket side. `pages[].page` er alltid
  fysisk, 1-basert sidenummer.
- En tett side deles i flere tekstbiter. `after_chunk` er 0-basert; hver bit har
  `chunk_count`, `byte_start` og `byte_end`.
- `limit` er et tak på 1–4 side- eller tekstvinduer. Svaret har dessuten høyst
  32 KiB UTF-8-tekst og 8 MiB base64-bilder, så det kan inneholde færre vinduer.
- Sidebildet leveres sammen med første tekstbit på siden. Agenten må lese bildet,
  særlig for skanninger og tabeller. Tomt tekstuttrekk er ingen grunn til å hoppe
  over bildet.
- `has_more=false` betyr slutten av leserekkefølgen, ikke full dekning. Bare
  `coverage.complete=true` bekrefter at alle tekstbiter og sidebilder i denne
  PDF-versjonen er levert. Hoppede biter vises i `coverage.unread_pages`.
  `coverage.complete` viser levert innhold, ikke at modellen forsto det.
- Uleselige eller ressursbegrensede sider står i `limitations` og
  `coverage.unreadable_pages`. Senere sider kan leses. Serveren blokkerer da full
  vedleggskvittering.

En side kan leses på nytt med ønsket fysisk side og `after_chunk=0`. Etter
komplett lesing brukes en ny forberedelse; originalen gjenåpnes fra
versjonsbundet cache uten ny nedlasting. Cachealder og opprinnelig hentetid
følger med.

En vert som bygger på serveren, kan slå på serverstyrt fortsettelse
(`attachment_flow`, av som standard), der serveren selv foreslår neste vindu.

## Implementasjon og binding

`pdf_windows.py` behandler én fysisk side ved behov. Poppler skriver til private
diskfiler; UTF-8 deles uten å splitte tegn. Tekstuttrekk i 150 DPI bevarer smale
tegn i tette sider. Lesbare dokumenter avvises ikke på grunn av samlet
tekstmengde (den gamle grensen på 2 MiB gjelder ikke her).

Cachen lagrer den avgrensede PDF-originalen og SHA-256, ikke ferdig uttrukne
sider. Leseren bruker render-versjon 7 (`RENDER_VERSION` i `live_attachments.py`;
den eldre leseren bruker 5), så gamle ferdigrendrede oppføringer brukes ikke.
Versjon 7 gjør et JPEG-bilde med flere innebygde bilder (MPO) til én side: bare
hovedbildet. Mobilkameraer legger HDR-kart, dybdekart eller forhåndsvisning inn
som ekstra bilder, og før versjon 7 ble de egne «sider».
Tekst- og arkivvedlegg følger sin egen kontrakt; se [mellomlagring](read-cache.md).

Forberedelsen bindes til kilde, referanse, vedleggs-ID, meldingens innholdshash,
konto og originalens SHA-256. Tekstbiter og sidebilder kontrolleres mot egne
hasher før levering. Lesegrunnlaget oppdateres først når hele verktøyresultatet
er bygget, så en feil underveis kan ikke kvittere ulest innhold.

Når `refresh=true` finner nye PDF-bytes under samme meldingsversjon og
vedleggs-ID, trekkes gammel lesekvittering for PDF-en tilbake. En eldre
forberedelse kan beskrive sin gamle original, men ikke kvittere den nye.
Konto- eller kildeendring, utløpte markører og uleselige sider gir heller ikke
komplett kvittering.

## Ressursgrenser og opprydding

| Grense | Verdi |
|---|---|
| Originalfil | 32 MiB |
| Fysiske sider | 100 (felles sidegrense) |
| Tekst på én side | 8 MiB |
| Sidebilde | 4 MiB |
| Arbeidsfiler per forberedelse | 128 MiB |
| Poppler per prosess | 30 sekunder, høyst 64 KiB diagnostikk |
| Alle PDF-dekodere i ett verktøykall | 55 sekunder fra kallets start |

Kildeadapterens egne nettverksfrister gjelder under innhenting. Dekoderens
prosessgruppe avsluttes ved ressursavbrudd. Ferdige, utløpte og lukkede
forberedelser ryddes; ufullstendige beholdes kort for videre lesing.

Grensene gjelder hvert verktøysvar og forberedelsen. De garanterer ikke mindre
samlet input til agenten.

## Logging

Leseren logger `pdf_window` på INFO-nivå (`source_server.py`) med antall vinduer,
tekstbytes, sidetall, dekning, uleselige sider og behandlingstid. Linjen har ingen
dokumenttekst, konto, kilde-ID eller markør. Den krever et loggoppsett som samler
INFO.

## Verifikasjon

`tests/test_archive_attachments.py` dekker tekst- og arkivvedleggene. Prosjektet
har ennå ingen egne tester for de trinnvise PDF-vinduene. For disse mangler
dermed selvstendig verifikasjon i dette prosjektet.
