# Flyt: innlogging og lesing

Flyt Foresatt er Vismas app for foresatte i barnehage, skole og SFO. Integrasjonen
bruker de samme API-rutene som Flyt Foresatt-appen for Android, og leser bare.
Innlogging og lesing av barnehage er prøvd mot en ekte konto i Oslo kommune
13. november 2026. Resultatet stemte med appen: beskjeder, dagbok med alle
bilder, kalender, meldingstråd, fravær og inn-/utsjekk.

**Innlogging.** Du velger kommune først. Kommunelisten
(`GET https://api.guardian.mobile.skole.visma.no/v1/municipalities`) er åpen og
krever ikke innlogging. Valget lagres i `.private/credentials/flyt-client.json`:
organisasjonsnummer, kundenavn (`customerName`), visningsnavn og
innloggingskontekst (`vismaConnectClientContext`). Innloggingen går til Visma
Connect med Authorization Code og PKCE (S256), og Visma Connect sender deg videre
til ID-porten. Kommunen følger med som `client_context` (kontekstverdien fra
listen, uendret) og `onbehalfof=vismaflytskole-<organisasjonsnummer>`. For Oslo
har kontekstverdien et annet nummer enn organisasjonsnummeret, og innloggingen
virket med nettopp denne kombinasjonen. ID-porten med BankID ba om fødselsnummer
i nettleseren, godkjenning i BankID-appen og deretter BankID-passordet i
nettleseren. ID-tokenet fra tokenutvekslingen må ha `iss` lik
`https://connect.visma.com`, appens klient-ID i `aud` og samme `nonce` som
innloggingen sendte; ellers lagres ingenting, og innloggingen må startes på nytt.
Signaturen kontrolleres ikke, siden tokenet kommer rett fra tokenendepunktet.
Etter tokenutvekslingen åpner integrasjonen én økt hos Flyt med
`POST v2/session` (`deviceId`, `osType`), slik appen gjør. Svaret er bare
status per system (`{"school": 3, "kindergarten": 1}` for en konto med bare
barnehage), uten økt-ID i svar eller hodefelt. Enhets-ID-en er tilfeldig og lagres i
`flyt-device.json`. Tokenene fornyes med Visma Connects `refresh_token` én gang
ved HTTP 401; et nytt refresh-token erstatter det gamle, og uten nytt beholdes
det gamle. Kontoidentiteten lagres bare som et SHA-256-avtrykk av `sub`.

**Hodefelt.** Hvert kall har `Authorization: Bearer`, `Customer-Key` (kommunens
`customerName`, for eksempel `oslo`) og `Accept-Language: nb-NO`. Lesing virker
uten `Session-Id`; integrasjonen sender den bare hvis `v2/session` en gang
returnerer en økt-ID. Vedlegg på API-verten hentes med bearer og `Customer-Key`.
Dagbokbilder ligger på `scdn.barnehage.visma.no` som signerte CloudFront-lenker
(`Policy`, `Signature`, `Key-Pair-Id`, gyldige i rundt ett døgn). De hentes uten
token og hodefelt, slik appen gjør; en utløpt lenke krever et nytt oppslag av
innlegget. Andre verter avvises.

**Lesing.** For hver barnehageplass (`kindergartenData.placementId` fra
`GET v1/children`):

| Innhold | Rute | Elementtype |
|---|---|---|
| Beskjeder | `POST vfk/v1/children/{plass}/announcements/search` | `announcement` |
| Dagbok | `POST vfk/v1/children/{plass}/diary-posts/search`, `GET .../diary-posts/{id}` for innlegg med bilder | `announcement` (emne «Dagbok») |
| Meldinger | `GET vfk/v1/children/{plass}/messages?Count=25&FromMessageId=` | `message` |
| Kalender | `POST .../calendar/events/counts`, `POST .../calendar/events/search` per dag, `POST vfk/v2/children/{plass}/calendar/events/{id}` | `event` |
| Fravær | `POST vfk/v1/children/{plass}/absences/search` | `absence` |
| Inn-/utsjekk | `POST vfk/v1/children/{plass}/attendances/search` (50 per side) | `attendance` (emne «Inn-/utsjekk») |

Barna leses fra `kindergartenData` i `GET v1/children`: `placementId` er nåværende
plass, og `placements` lister alle plassene (den nåværende også). Alle plassene
leses.

Det integrasjonen tar hensyn til fra ekte svar:

- Dagboksøket viser bare det første bildet, og bare som miniatyr
  (`thumbnailUrl`, 150x150). Detaljoppslaget har alle bildene med `previewUrl`
  (1920x1920), så integrasjonen henter detaljen for hvert innlegg med bilder i
  vinduet (høyst 200 per kjøring). `get_item` for dagbok er ett detaljoppslag.
  Listen bygger et innlegg med bilder fra detaljen alene, ikke blandet med
  søkeraden, så listen og `get_item` gir samme element og samme innholdshash.
  Uten bilder brukes søkeraden; den hadde de samme feltene som detaljen.
- `get_item` for en kalenderhendelse søker først på forekomstens norske dato og
  så på UTC-datoen. En heldagshendelse starter kl. 22 eller 23 UTC dagen før.
- Beskjedsøket svarer `totalCount: 0` ved siden av ekte rader. Totalen avslutter
  derfor bare blaingen når den er minst like stor som det som allerede er lest;
  ellers stopper en side som ikke er full.
- Kalenderens `counts` tar ikke med dagen i `to`. Integrasjonen sender derfor
  midnatt etter siste dag (`to=2026-12-02T00:00:00Z` for et område som slutter
  1. desember). Samme dag i `from` og `to` gir «Date range is invalid».
  `childIds` er barnets `childId`, ikke plass-ID-en.
- Søkene kommer nyeste først. Tidspunkter er UTC med eller uten
  millisekunder.

Søkene er `POST`, men leser bare. `flyt.READ_ROUTES` lister metode og eksakt
sti for hver lesing; alt annet stoppes før nettverket. Det gjelder blant annet
`PUT .../announcements/{id}/seen` og `POST .../messages/seen` (merker som lest),
`POST .../messages` (sender), fravær (`POST/PUT/DELETE .../absences`), samtykker,
`register-device` og `DELETE v2/session`. Lesing endret ikke ulest-status:
`GET v1/children/badges` og `.../announcements/unread-count` var like før og
etter lister, detaljoppslag og bildenedlasting, og `seenDate` på beskjedene var
uendret. Selve beskjedens detalj-GET (`.../announcements/{id}`) brukes ikke,
fordi den ikke er prøvd på en ulest beskjed. Grenser: 25 per side, høyst 20 sider og
500 elementer per strøm og barn, kalender fra starten av vinduet til 60 dager
fram (høyst 62 dager med hendelser og 200 hendelsesdetaljer), 20 MB per vedlegg
og 100 MB per nedlasting. Når en grense nås eller en strøm feiler, blir listen
merket ufullstendig (`complete=false`), men resten av innholdet leveres.

**Skole og SFO** (`vfs/`-rutene) er ikke med ennå. Nye ruter legges til i
`READ_ROUTES` og får egne hentefunksjoner i `flyt.py`.

**Ikke kontrollert ennå** (kontoen som ble prøvd, hadde ikke slikt innhold):

- Hvordan `FromMessageId` blar i en meldingstråd (tråden var tom), og hvordan en
  melding ser ut.
- Hvor vedlegg til beskjeder og kalenderhendelser ligger. API-verten og
  bildeverten godtas; en annen vert gir en tydelig feil.
- Fravær, flere barn, flere kommuner og skole/SFO.

Skriving er ikke implementert: ingen meldinger, fravær, samtykker eller
«lest»-markering.
