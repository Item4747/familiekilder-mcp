# Funksjoner og API-verifikasjon

Hva hver tjeneste støtter, hvilke API-kontrakter som er verifisert, og hvordan
skrivehandlingene er vernet. «Ikke verifisert» betyr at vi mangler nok bevis for
kontrakten og for kontroll av resultatet. Det betyr ikke at tjenesten mangler
funksjonen. Ingen kategori merkes «ikke støttet» uten slik dokumentasjon.

| Tjeneste | Direkte lesing implementert | Meldinger/svar | Fravær | Deltakelse |
|---|---|---|---|---|
| Vigilo | Barn/enheter, mottakere, tråddeltakere, meldingstråder, nyheter, hentemeldinger/repeterende henteavtaler, fravær/koder, møteinvitasjoner/tider, påmeldingsskjemaer/valg, vedlegg | Implementert: ny tekstmelding og svar | Implementert: heldagsfravær | Implementert: møter og påmeldingsskjemaer |
| Skolemelding | Barn/skoler, lærermottakere, innboks/sendtmappe, detaljer/tråder, vedlegg; lesing etter prosessomstart og tre tokenrotasjoner (to eksplisitte, én automatisk) er live-verifisert | Implementert: ny tekstmelding til én lærer. Svar ikke verifisert | Implementert: fraværsmelding, ikke føring i skolens register | Ikke verifisert |
| Spond | Profil/grupper, innlegg, aktiviteter, komplette kommentartrær, varsler, vedlegg; chatmottakere, paginert historikk og chatvedlegg | Implementert: ny tekstchat, chatsvar og svar som kommentar | Ikke verifisert | Implementert |
| Hoopit | Profil/egne barn, innlegg/kommentarer, aktiviteter/kommentarer, egne deltakelsessvar, varsler, vedlegg; egne chatter, paginert meldingssøk, trådsvar og chatvedlegg, kontaktgrupper og chatmottakere | Implementert: ny tekstchat, chatsvar og svar som kommentar | Ikke verifisert | Implementert |
| IST Home | Barn/plasseringer, direkte Innlegg, paginerte samtaler/mottakere, fravær, vedlegg/bilder, samtalebookinger/tider | Implementert: ny tekstmelding og svar | Implementert | Implementert: bestille/avbestille samtaletid |

## Felles regler for skrivehandlinger

- Bare verifiserte kontrakter kan registrere et skriveverktøy (`live_actions.py`,
  `verified`/`implemented` i `sources.REGISTRY`). Rettighetene står som en
  `write`-liste per tjeneste i `.private/mcp-config.json`, med kategoriene
  `messages`, `absence` og `participation`. En rettighet gir ikke API-støtte i seg
  selv. Gmail kan bare lese.
- `family_connections` viser både implementasjonsstatus og tildelte rettigheter.
  `capabilities.actions` viser status per handling, slik at et implementert svar
  ikke forveksles med støtte for en ny samtale.
- Hvert skriveverktøy krever sin egen kategori og en konkret beskjed fra brukeren.
  Tekst fra kilden autoriserer aldri en handling.
- Kontrakt betyr her rute, felter og format slik leverandørens egen app eller
  nettklient faktisk sender dem. Integrasjonene er uoffisielle: de bruker de samme
  API-ene som tjenestenes egne apper, med brukerens egen innlogging. Lesing er
  kontrollert med live GET der det står. Skriving er bare testet mot syntetiske
  mål: ingen skrivehandling i denne filen er live-testet mot en ekte kilde.
  Live skrivetest krever et testmål (testkonto eller testsamtale) som brukeren
  oppgir; se [ny kildetjeneste](adding-a-service.md).

### Usikkert utfall

Ingen av tjenestene har en verifisert idempotensnøkkel (en nøkkel som lar
serveren avvise duplikater). Alle skriveverktøy bruker derfor
`ledger.reserve_operation`/`operation_result` med disse reglene:

- Operasjons-ID (UUID fra agenten) og et fingeravtrykk av innholdet lagres før
  sending, sammen med ID-ene til identiske objekter som allerede finnes.
- Timeout, avbrudd, nettverks- og HTTP-feil (også 401/403/429), omstart og
  manglende eller tvetydig kvittering gir utfallet `unknown`. Sendingen gjentas
  aldri automatisk.
- Samme operasjons-ID gjør bare kontrolloppslag, også etter omstart og etter
  kildens trafikkpause.
- En ny UUID med samme uavklarte fingeravtrykk avvises. Kontobytte kan verken
  omgå sperren eller verifisere handlingen med en annen avsender.
- Registeret lagrer mål-ID-er, operasjons-ID, fingeravtrykk, fase og
  kvitteringsreferanser. Meldingstekst, titler, merknader og skjematekst lagres
  aldri.
- Kvittering krever et ferskt oppslag etter sending. For meldinger og fravær må
  det finnes nøyaktig ett nytt objekt (ny ID) med riktig avsender, mål og innhold.
  For deltakelse må fersk status vise svaret. Identiske objekter som fantes før
  sending teller ikke, og en vellykket HTTP-retur alene er ikke kvittering.
- Handlinger med flere trinn (ny samtale + første melding) har deterministiske
  deloperasjons-ID-er avledet av UUID-en. En omstart fortsetter bare trinn som
  aldri ble sendt; et sendt eller uavklart trinn kontrolleres uten ny sending.

Avsnittene under beskriver bare det som er særskilt for hver tjeneste.

## Vigilo

Integrasjonen bruker de samme API-rutene som Vigilos Android-app. Barn, enheter,
mottakere, tråddeltakere, fraværskoder og fraværsliste er kontrollert med live
GET. Hvert kall sender bare bearer-token og appVersion, ingen `userId`-query.

### Meldinger og fravær

- `vigilo_action_context`: barn/enheter fra `GET api/children`, person-ID-er fra
  `GET api/messages/contact-list` og aktive koder fra `GET api/absencecodes`.
  Personens `id` brukes, ikke `employeeId`. Kontaktgrupper viser konkrete medlemmer.
- `vigilo_thread_context`: deltakere fra `GET api/message-threads/{id}/participants`
  og svarrettighet fra `GET api/messages/threads/{id}`. Ingen lest-markering.
- `vigilo_send_message`: `POST api/messages` med `title`, `body`, tom `attachments`,
  `organizationalUnitId` og mottakere `{type, externalId}`. Barnet legges til som
  `client`, valgt ansatt som `employee`, foresatt som `legalGuardian`. Skole krever
  minst én ansatt. Resultatet kontrolleres mot ny tråd, avsender, tekst og mottakere.
  Kontrolloppslag ved ukjent resultat går bare 14 dager tilbake; eldre uavklarte
  handlinger forblir uavklarte og sendes ikke på nytt.
- `vigilo_reply_message`: `POST api/messages/{threadUid}/reply` med `body` og tom
  `attachments`. Validerer barn, enhet, innlogget deltaker og `isReplyDisabled`.
  Henter hele tråden på nytt for kvittering med melding-ID.
- `vigilo_register_absence`: `POST api/absences` (barnehage),
  `api/student-absences` (skole) eller `api/after-school-absences` (SFO).
  Velg barn/enhet og inklusive ISO-datoer; appen sender datoene som `dd.MM.yyyy`.
  Barnehage krever aktiv `absenceCodeId`. Skole krever merknad, tittel og ansatte
  som mottakere. SFO bruker merknad. Skole og SFO bruker ikke valgt fraværskode.
  Fraværet pagineres før sending, og overlapp avvises. Etter sending kontrolleres
  ny fraværs-ID, avsender, barn, enhet, datoer, merknad og eventuell kode.

Alle verktøy håndhever egne `messages`-/`absence`-rettigheter og enhetens
funksjonsflagg.

### Hentemeldinger og repeterende hente-/hjemsendingsavtaler

`vigilo_list_pickup_notes(child_id, on_date?, organization_id?)` leser
`GET /api/check-in-notes?childPersonId=...&fromDate=YYYY-MM-01&toDate=...`.
`on_date` er en ISO-dato, standard er dagens dato i Europe/Oslo. `toDate` er
første dag i neste måned og eksklusiv, slik appen spør; `through_date` er siste
inklusive dag. Valgfri enhet valideres mot eget barn og brukes til lokal
filtrering; andre egne enheter eller kategorier overstyrer aldri hverandre.

- Dato-/tidsstrenger uten offset tolkes som UTC, og datoer konverteres til
  Europe/Oslo. Hentetiden `timeOfDay` er lokal klokketid.
- Kildens `dateOccurrences` avgjør hvilke dager hver avtale gjelder, også ved
  ferier og annenhver uke. Verktøyet utvider aldri repeteringsreglene selv.
- `items` bevarer henteform, merknad, klokkeslett, periode og repeteringsmønster.
  `selections` velger nyeste `lastUpdatedTime`, med `registeredTime` som reserve,
  per dato/enhet/kategori, slik appens kalender gjør. `selection_status` skiller
  `current`, `superseded` og `not_for_date`. Lik tidsverdi gir `ambiguous`;
  deaktivert nyeste melding gir `disabled` uten å gjenopplive en overstyrt avtale.
  Ingen av disse to gir en bekreftet gjeldende avtale.
  [Vigilos SFO/AKS-veiledning](https://vigiloskole.zendesk.com/hc/no/articles/14648975062684-Hentemelding-SFO-AKS)
  beskriver gjeldende og overstyrte avtaler i ukeoversikten.
- `kind=pickup_note`-referanser kan leses ferskt med `vigilo_get_item`, som
  beregner gjeldende status på nytt, med observasjonstoken som andre
  oppslag. Det er et målrettet oppslag, ikke en ny automatisk endringsstrøm.
- Lesetilgang er nok. Ingen hentemelding opprettes, endres, slettes eller
  markeres lest.
- Hele månedssvaret kreves. Ukjent sidedekning, ugyldige datoer, dupliserte ID-er,
  kontobytte, andre barns/enheters data eller overskredne lesegrenser avvises.
- Manglende treff beviser ikke at det ikke finnes en avtale. Planlagt hentetid
  dokumenterer ikke faktisk utsjekk eller plassomfang.

Bare syntetisk testet; live-verifisering av hentemeldingssvar gjenstår.

### Møteinvitasjoner og på-/avmelding

`vigilo_list_meetings` leser invitasjoner, konkrete møtetider, eget svar og
vedleggsreferanser. `vigilo_get_item` henter en fersk invitasjon med dens
`reference`; `vigilo_read_attachment` bruker samme ferske oppslag og vanlig
vertsvalidering, behandling og opprydding. Oppfølgings-ID inkluderer møte, barn
og innlogget foresatt, slik at ulike invitasjoner ikke blandes.

Kontrakten:

- `GET api/forms/meetings/invitations/{childId}`;
  `POST api/forms/meetings/{meetingId}/replies` med `childPersonId`,
  `inviteePersonId`, `meetingOccurrenceId`;
  `DELETE api/forms/meetings/{meetingId}/replies/{personId}/{childId}`.
- Foresatt er den innloggede, det vil si tokenets `personId`.
- Svaret har `items`, `meetingOccurrences`, `registrationDeadline` og eksplisitt
  null eller objekt i `reply`. Et møte har én tid (`singleMeetingTime`) eller
  flere (`multipleMeetingTimes`). Svaret identifiseres med `meetingOccurrenceId`
  og `replyTime`.
- Endringer er bare tillatt før fristen, eller når fristen er null. Tidene tolkes
  som UTC.
  [Vigilos appveiledning](https://vigilo.zendesk.com/hc/no/articles/27432336442652-App-P%C3%A5melding)
  bekrefter valg av møtetid og at endringer stopper ved påmeldingsfristen.

`vigilo_set_participation` krever `participation`-rettighet, UUID, barn-ID,
møte-ID, forekomst-ID og `accepted`. Handlingen gjelder innlogget foresatt på
vegne av valgt barn, aldri en annen foresatt. Barnets enhet, frist, tid og
tilgjengelighet kontrolleres, og hele invitasjonen leses på nytt rett før sending.
En annen valgt tid flyttes ikke automatisk. Kvittering krever at eksakt møte,
forekomst/tid, enhet og svar gjenfinnes. Manglende invitasjon, manglende
`reply`-felt eller kildesvikt er aldri bevis for avmelding.

API-modellen har ingen sidemarkør. Resultatet beskriver hele invitasjonssvaret nå,
uten historikk- eller endringsgaranti. Ukjent responsform eller mer enn 5000
invitasjoner/tider avvises.

Live: rutene for møter og generelle registreringer svarte med 0 invitasjoner for
eget barn. Konkrete invitasjonsobjekter og skriving er derfor bare kontrakttestet.
Generelle registreringsskjemaer har egne verktøy (under) og vises ikke som møter.

### Påmeldingsskjemaer med enkeltvalg, flervalg og rangering

`vigilo_list_registrations` leser skjemaer, alternativer, skjematype, frist og
innlogget foresatts svar. `vigilo_get_item` og `vigilo_read_attachment` støtter
skjemareferansene på samme måte som andre Vigilo-referanser.

Kontrakten:

- `GET api/forms/registrations/invitations/{childId}` gir `items` uten sidemarkør.
  Ingen historikk- eller endringsgaranti gis.
- `POST api/forms/registrations/{registrationId}/replies` tar `childPersonId`,
  `inviteePersonId` og `selectedRegistrationChoices`. Hvert valg har
  `registrationChoiceId` og `rank`.
- `singleChoice` og `multipleChoices` sender opprinnelig `choiceNumber` som
  `rank`; ikke renummerer. Bare `rankedChoices` bruker brukerens rekkefølge med
  rang fra 1, og en ny rangert påmelding tar med alle alternativer.
- Tomt valg er
  `DELETE api/forms/registrations/{registrationId}/replies/{personId}/{childId}`;
  det sendes ikke POST med tom liste. Foresatt-ID og barn må tilhøre den valgte
  innloggingen.
- Svaret har `replyTime` og `selectedRegistrationChoices` med
  ID og rang. Avmelding krever eksplisitt null i `reply` på et skjema som fortsatt
  er tilgjengelig. Manglende treff er ukjent utfall.

`vigilo_set_registration` krever `participation`-rettighet, UUID, barn, skjema-ID,
skjematype fra oppslaget og hele ønskede `choice_ids`-listen. Valgene valideres
mot ferske alternativer. Tom liste trekker svaret tilbake. Eksisterende svar kan
endres før fristen. Invitasjonen kontrolleres igjen før én sending. Flervalg
sammenlignes som et sett i duplikatkontrollen; rangering beholder rekkefølgen.
Ferskt svar må ha eksakte valg/ranger, konto og enhet. Fordi bare fingeravtrykket
av valgene lagres, kan endret betydning etter sending ikke gi falsk kvittering.
Oppfølging skiller skjema, barn og foresatt.

Live: leseverktøyet virket, men fant 0 skjemaer. Konkrete skjemaobjekter og
skriving er derfor bare kontrakttestet; live skrivekontroll krever en aktiv
testinvitasjon.

## Skolemelding

Integrasjonen bruker de samme API-rutene som Skolemelding-appen for Android.

### Barn, skoler og lærermottakere

`skolemelding_action_context`, `skolemelding_recipients` og
`skolemelding_thread_context` er lesende verktøy og virker uten skriverettigheter,
meldingsinnhenting eller arkiv.
De henter ferske data gjennom den vanlige autentiserte klienten, kildelåsen og
trafikkbegrensningen.

- Barn identifiseres med `DomainUser.UserName`, skoler med `AvailableSchools[].Code`.
  `DependentHasTeacherConnectedPerSchool` viser `SchoolCode` og boolsk
  `HasTeacherConnected`; manglende eller ukjent verdi blir aldri en tillatelse.
- `GET /api/MyChildsTeachers?schoolId=...&childId=...` returnerer `Users`.
  Barnets `DomainUser.UserName` er `childId` og skolens `Code` er `schoolId`.
  Lærerens `Username` er mottaker-ID, `FullName` er visningsnavn og `IsMine`
  angir tilknytning. Ved sending blir `Username` til `UserRecipients[].Id`; et
  annet `Id`-felt skal ikke brukes.
- Ingen paginerings- eller totalantallskontrakt er bekreftet. Resultatet oppgir
  derfor begrenset dekning og er ikke en full katalog over mulige mottakere.
- Ugyldige identiteter, motstridende skoletilknytning og ukjente barn/skoler
  avvises. API-et gjentar lærere med ulike grupper og `IsMine`-flagg; like
  brukernavn med samme navn samles. Motstridende flagg gir `is_associated: null`,
  `association_ambiguous: true` og en begrensningsmelding. Motstridende eller
  manglende navn ved duplikat avvises. Ingen skriverett utledes. Bare
  normaliserte identiteter og navn returneres.

Live-verifisert: barn og læreroppslag, inkludert motstridende tilknytninger som
vises uavklart. Innlogging, tokenrotasjon og lesing i ny prosess er beskrevet i
[auth-lifecycle.md](auth-lifecycle.md). Maksimal øktlevetid er ikke målt.

### Eksplisitt trådvalg

`skolemelding_thread_context(reference, thread_id?)` tar en meldingsreferanse fra
listeoppslaget. Det validerer eget barn gjennom fersk profil og henter
`GetMessageWithId` med tom `threadId` for trådoversikt. Detaljoppslag bruker bare
en kjent `ThreadId`. Ved flere tråder uten valg vises tråd-ID-ene, men ikke
deltakere eller svar fra en tilfeldig standardtråd. Én tråd kan velges automatisk.
`ThreadId` kan også ligge i `Replies` når `Threads` mangler. API-et har returnert `Threads: null`; det behandles som
manglende liste, mens andre ugyldige listetyper avvises. Trådmedlemmer vises med
`UserId` og `Name`.

Feil meldings-/barn-ID, feil svartråd, duplikate identiteter, motstridende
referansefelter og store svar avvises. Svarflagget i responsen er informativt,
ikke en verifisert skriverettighet. Komplett svarpaginering er ikke bekreftet,
vedlegg leses separat, og `complete`/`actions_verified` er false. Oppslaget gir
derfor ikke observasjonstoken for fullstendig oppfølging. Live-verifisert med en
tråd med flere medlemmer og svar.

### Sendtmappe

`skolemelding_list_sent_messages(child_id, page=1)` leser meldingssøket
`POST /api/messagesearchmobile/`, med `SearchInbox=false` for sendt og `true` for mottatt. Sidene starter på 1.

- Barnet verifiseres fra fersk profil, og hver forespørsel går til API-et.
- Returnerer høyst 50 sammendrag per side, oppgitt total, `has_more` og
  `next_page`. Sidegrense 200. Korte eller motstridende sider og ukjent total vises.
- Manglende `Result`, feil barn og duplikate meldinger avvises. Detaljer og
  vedlegg leses separat, og observasjonene er ufullstendige (`complete=false`).
- Live-verifisert: hele sendtmappen ble lest med `has_more=false`.

`GET /api/sentmessages` er ikke brukbar som kvittering: live GET ga HTTP 404.

### Ny melding til lærer

En ny melding er `POST /api/message` med `Header`, `body`, `OnBehalfOf`,
`SchoolId`, `GroupRecipients`, `UserRecipients`, `Attachments`,
`ExistingFileIds`, `IsReplyAllowed`, `IsOtherUserAdded`, `FeedbackRequired`,
`FeedbackQuestionRequired`, `IsSmsMessage`, `SmsNotification`, `IsAbsenceReport`,
`HideReceivers` og `MessageImages`. `IsReplyAllowed` er sann når avsenderen ikke
er lærer. Teksten sendes som HTML. Mottakeren er lærerens `Username` som `UserRecipients.Id`, valgt `Name`
og tom `ParentIds`, med de statiske feltene `Id=null`, `Name=null`,
`UserRecipientsToInclude="None"`, `TeacherCount=0`, `StudentCount=0` og
`ParentCount=0`.

`skolemelding_send_message` krever `messages`, eget barn/skole fra fersk profil og
én stabil `Username` fra læreroppslaget. ID-en må ha et entydig navn; likt navn er
aldri nok til å velge person. Ingen annen foresatt, gruppe, SMS, vedlegg eller
tilbakemeldingskrav legges til. Teksten HTML-escapes. Tittelgrense 200 og
tekstgrense 10000 er lokale grenser, ikke API-ets maksimum.

Kvittering bygger på full, avgrenset gjennomgang av sendtmappen
(`SearchInbox=false`) før og etter sending: stabil `TotalMatching`, ingen
duplikater, høyst 200 sider à 50. For lik tittel og normalisert tekst hentes
detaljene. Kvittering krever `IsSender=true`, eksakt eget barn og skole,
`ReceiverId` lik den ene valgte læreren, ingen fraværsflagg, vedlegg eller bilder,
og nøyaktig én ny meldings-ID. Sammensatt `ReceiverId`, andre mottakere eller
flere nye treff gir aldri verifisert kvittering. Kvitteringen har mål, kilde-ID og
tidspunkt. HTTP 401/403/429 gir `unknown` uten automatisk tokenfornyelse. Etterkontrollen er live-verifisert mot en eksisterende melding.

### Fraværsmelding

En fraværsmelding er `POST /api/message` med `IsAbsenceReport=true` og tomme
`UserRecipients` og `GroupRecipients`. Den krever `HasTeacherConnected` for eget
barn og egen skole. Perioden står i teksten med `DD.MM.YYYY`, eventuelt `HH.mm`
eller «Hel dag», med samme norske tittel og tekst som appen bruker. Dette er en
fraværsmelding, ikke en strukturert fraværsføring.

Osloskolens brukerveiledninger for ansatte og foresatte beskriver at tjenesten
ruter meldingen til kontaktlærer og skolens fraværsgruppe, og at ansatte må føre
fraværet i sitt skolesystem. Det er derfor meldingen som kvitteres; `attendance_registration_verified` er alltid
false.

`skolemelding_report_absence` krever `absence`-rettighet, eget barn/skole og
eksplisitt `HasTeacherConnected=true`. Det sender `IsOtherUserAdded=false` og ingen
fritekst/årsak, vedlegg, SMS eller annen foresatt.

- Begge datoer er inklusive og skrives `YYYY-MM-DD`. Klokkeslett krever både start
  og slutt som `HH:MM` i Europe/Oslo. Ugyldige eller reverserte perioder og
  tvetydig eller ikke-eksisterende sommertid avvises. Maksimum 366 dager er en
  lokal grense. Datoer er alltid eksplisitte, aldri et dynamisk «i dag».
- Formatet følger appens datointervall: én dato med klokkeslett, to datoer med
  klokkeslett, eller fra/til-dato uten klokkeslett.
- Kvittering bruker samme gjennomgang av sendtmappen som ny melding, men med
  `IsAbsenceReport=true` og tjenestestyrte mottakere. Barn, skole, periode, tekst
  og `IsSender` må stemme, og det må finnes nøyaktig én ny ID. Kvitteringen viser
  periode, skole, barn og meldingsreferanse.
- Endret visningsnavn kan ikke omgå sperren for en uavklart melding for samme
  barn og periode.

Live lesing gjenskapte teksten i en eksisterende fraværsmelding og fant den
gjennom etterkontrollen.

### Svar: ikke verifisert

- `POST /api/threadmessage` med `MessageId`, `ThreadId`, `RecipientId` (fra
  meldingens `ReceiverId`), `MessageText` som HTML, `OnBehalfOf`, `SenderName`,
  `isReplyAllowed`, `AddOtherParentInThread`, `TeacherRecipients` og
  `AddOtherTeacherInThread`.
- Svaret på POST brukes som tråd-ID når en ny tråd opprettes, ikke som kvittering
  for ett sendt svar. Det kan ikke alene bevise levering.
- `Replies` har `ThreadId`, `AuthorId`, `SenderName`, `MessageText` og `Timestamp`;
  `AuthorId` svarer til `ThreadMembers.UserId`. Profilens
  `DomainUser.UserName` samsvarer ikke med noen `UserId`/`AuthorId` i egne
  meldinger: feltene bruker ulike ID-typer. `ReceiverId` kan inneholde flere
  kommaseparerte identiteter.
- Appen selv kjenner igjen egne svar på visningsnavn. Det er ikke en stabil
  avsender-ID, og det brukes ikke her.
- Ingen stabil avsender-ID er funnet (også `GetChildsParent`,
  `userSearch/GetParents` og access-tokenet, som er opakt og ikke en JWT, er
  undersøkt). Svar forblir uverifisert.

`GET /api/Message/RegisterAbsentMessage?messageId=...` er en handling, selv om
metoden er GET. `absentRegister` og `absentComment` er ikke fraværslesing, og de
tillatte leserutene utelukker begge.

## Spond

Kontraktene er lest ut av [Sponds egen nettklient](https://spond.com/client/),
JavaScript-ressursen `client-main-8311edbbc48d9114f151.js`, hentet 30.09.2026. Spond har egne trafikkgrenser og en varig
trafikkpause som også gjelder kontrolloppslag.

### Lesing: 401 og 403

Etter én vanlig tokenfornyelse betyr 401 at brukeren må logge inn på nytt. 403
betyr bare at denne ressursen ikke er tilgjengelig (`SourceUnavailable`), og
lesingen fortsetter. Arrangementsdetaljer som hentes på grunn av et varsel og
ikke er tilgjengelige, gir `complete=false`, så ufullstendig dekning vises.
Lærdom: å tolke 403 som utlogging stoppet hele gjennomgangen. Årsaken til slike
403 er ikke fastslått.

### Deltakelse

`spond_participation_context` gir valgbare identiteter før sending, også med bare
leserettighet. `spond_set_participation` følger klientens `respondEvent`/`respond`:

- Les profil og grupper for å finne egen person og barn med foresattkobling.
- Les `GET /core/v1/sponds/{event_id}` for arrangement og nåværende svar.
- Skriv `PUT /core/v1/sponds/{event_id}/responses/{member_id}` med `accepted`
  som boolsk felt.
- Les arrangementet på nytt. Status ligger i `responses.acceptedIds`,
  `declinedIds`, `waitinglistIds` og `unansweredIds`. Venteliste rapporteres som
  venteliste.

Bruk stabile ID-er. Betaling og vertshandlinger inngår ikke, og
`X-Spond-SkipPayment` sendes ikke. Vanlig API-token fra innloggingen brukes;
serveren håndhever `participation`-rettigheten separat. Leverandørens frist- og
tilgangsregler gjelder.

### Svar i innlegg og aktiviteter

Klienten bruker `addPostComment`/`addEventComment` og
`getPostComments`/`getEventComments`. `commentModelFactory` gir kommentar-ID,
`fromProfileId`, `timestamp` og nestede `children`.

- `spond_thread_context` leser detaljruten og `GET /core/v1/posts/{id}/comments`
  eller `sponds/{id}/comments`. Ruten returnerer hele kommentartreet som en liste;
  appens «vis flere» utvider bare den hentede listen. Ukjent responsform,
  dupliserte ID-er, ukjent svarnivå eller mer enn 1000 kommentarer avvises.
- Resultatet viser gruppetilknytning/synlighet, kommentarer, hovedkommentar-ID og
  `commentsDisabled`. Skriving krever eksplisitt `false` fra kilden og
  `messages`-rettighet. Lesing markerer ikke noe som lest og varsler ingen.
- `spond_reply_message` sender `POST` til samme kommentarrute, eventuelt med
  `/{parent_comment_id}` for svar under en bekreftet hovedkommentar. Feltene er
  `text` og `notifications: {mentions: null}`; aktiviteter får også
  `hostsOnly: true`. Svaret er synlig for trådens deltakere, selv om varslingen
  begrenses til verter. Omtaler, bilder, privat chat og nye meldinger inngår ikke.
- Maks 4096 tegn, fersk avsender fra `GET /core/v1/profile`, riktig
  tråd-/foreldrekobling og fersk svarrettighet kontrolleres før sending. Kvittering
  krever én ny kommentar-ID med samme tekst, avsender og forelder.

Live GET har bekreftet begge kommentarruter, svarflaggene og kommentarform/avsender.

### Chat: lesing

`spond_chat_recipients`, `spond_list_chats` og `spond_get_chat` er lesende verktøy.
Kontraktene kommer fra klientens `ChatFactory`, den moderne chatklienten og
meldings- og chatpagineringen.

- En kortvarig chatøkt opprettes med `POST /core/v1/chat` og tomt objekt. Svarets
  URL må være nøyaktig `https://api.spond.com/chat/v1`. Chatnøkkelen holdes bare
  i minnet og sendes som `Auth` sammen med vanlig bearer-token og `Api-Level`.
  Denne POST-en autentiserer lesing; den sender ingen melding.
- `GET /chats/groupRecipients/{groupId}` gir standardmottakere som profil-ID-er.
  Gruppen valideres mot ferske egne grupper. Aldersgrenser omgås ikke.
- `GET /chats?max=20&maxTimestamp=...` henter egne DM- og gruppechatter til en
  tom side. Kildens tidsmarkør kan begrense dekningen ved like tidspunkt eller
  samtidige endringer; verktøyresultatet sier fra om dette.
- `GET /chats/{id}` og `/chats/{id}/messages?count=20&maxNum=...` henter
  deltakere og meldinger. `maxNum` er inklusiv, så grensemeldingen fjernes fra
  neste side. Null betyr nyeste side i query, mens melding nummer null finnes.
  Derfor trekkes ikke én fra sidemarkøren.
- Vedlegg og videresendt innhold markeres som ulest, og `attachments_complete`
  blir false. Bruk meldingens referanse med `spond_read_attachment`. Videresendte
  innlegg og aktiviteter har egne kildereferanser og hentes separat. Foresattes
  mulige innsyn beskrives. Leseverktøyene markerer ikke noe som lest, reaktiverer
  ikke, sender ikke og arkiverer ikke lokalt.

Live lesing bekreftet chatlisten til tom side og meldingspaginering til nummer null.

### Chat: nye meldinger og svar

`spond_send_message` og `spond_reply_chat` krever `messages`-rettighet.
Kontraktene kommer fra klientens `sendMessage`,
`getThreadWithRecipients`/`createMessage` og skjermens `performSend`.

- `POST /chats/find/{groupId}` med en JSON-liste av mottakernes profil-ID-er er
  et oppslag før sending; det oppretter ikke chat. Tomt objekt betyr at ingen chat
  finnes, slik klienten tolker det. Live oppslag ga riktig chat-ID, gruppe og
  deltakersett for en kjent samtale.
- Nye meldinger velger 1–99 konkrete standardmottakere fra ferskt gruppeoppslag.
  Egen gruppe, avsenderens medlems-/foresattkobling og vanlig aldersrettighet
  kontrolleres. Aldersomgåelse og usikker fødselsdato med aktiv grense støttes ikke.
  Foresatte eller andre mottakere legges ikke til automatisk.
- `POST /chat/v1/messages` tar `text`, `type: TEXT` og tom `mentions`.
  Eksisterende chat bruker `chatId`. Ny chat bruker `groupId` og enten
  `recipient` (én mottaker) eller `recipients` (flere). Ingen bulk-rute, bilder,
  filer, betalingsinnhold, reaktivering eller medlemsendring brukes.
- Ferske deltakere og gruppe kontrolleres før sending. Sist observerte
  meldingsnummer lagres før én POST. Kvitteringens `chatId` og `msgNum` lagres
  straks, før flere oppslag.
- Etter sending leses alle nyere meldingssider tilbake til det lagrede nummeret
  eller kildens slutt. Én ny tekstmelding må stemme med avsender, tekst, chat,
  mottakere og eventuell leverandørkvittering. Flere identiske nye treff, endret
  publikum eller ufullstendig kontroll gir `unknown`. Kontroll over 250 sider
  (opptil 5000 rader før overlapp) avbrytes som `unknown`.
- Mangler kvitteringen for en ny chat, brukes det verifiserte mottakeroppslaget
  (`/chats/find`) for å finne chatten igjen.

### Chatvedlegg og oppfølging

Klienten bruker `message.file`, flertallsfeltet `message.images` og eldre
`message.image`; filens `media` går gjennom gruppens sikre filoppslag. Live lesing
bekreftet bilder med `url`, `height` og `width` i `images`. Slike bilder teller
som vedlegg og markeres som ulest.

- Chatmeldinger får stabile referanser `{kind: chat_message, chat_id, msg_num}`,
  vedleggs-ID-er og observasjonstoken. `spond_get_item` henter meldingen igjen,
  også nummer null. Meldings- og chat-ID valideres.
- `spond_read_attachment` finner filen i et nytt meldingsoppslag og bruker vanlig
  vertsvalidering, gruppetilknytning, trafikkgrenser og bytegrense. Agenten får
  filnavn, type og ID, aldri den interne nedlastingslenken. Endret signatur
  beholder vedleggs-ID; et erstattet eller fjernet vedlegg avvises.
- Filer gir tekst og sidebilder gjennom vanlig renderer. Midlertidige filer slettes
  etter siste side eller ved feil; rester etter avbrudd fjernes av oppryddingen
  ved oppstart og hver 24. time.
- Ukjent mediestruktur, slettede meldinger og ulest videresendt innhold gir
  `content_complete=false`. Kjente vedlegg er ikke lest før de er hentet med
  `spond_read_attachment`.

Live lesing gjennom verktøyet hentet et chatbilde og ryddet etter seg.

## Hoopit

Kontraktene er lest ut av [Hoopits nettklient](https://app.hoopit.io/main.dart.js),
hentet 01.10.2026. Funksjonsnavnene der er minifisert, så bare ruter og felter
står her. Chat går via Stream Chat 10.4.0 og følger Streams dokumentasjon.
Hoopit har egne trafikkgrenser og pauser, som også gjelder chatkall.

### Deltakelse

`EventResponseStatus` sendes som verdi, ikke som enumens indeks: 0 ubesvart,
1 avslått, 2 godkjent, 4 venteliste, 5 kanskje. Oppmøteregistrering og
organisatorsvar har egne ruter og inngår ikke.

- `hoopit_participation_context` virker også med bare leserettighet. Det henter
  `GET /app/users/current/` v6, `current/children/` v1, aktivitetens detaljrute v12
  og `GET /app/events/{event}/occurrences/{occurrence}/my/responses/` v1.
  Deltakerne filtreres mot innlogget profil og egne barn. Resultatet har
  person-ID, svar-ID, status og `can_edit_response`. Live-verifisert, også for en
  eldre aktivitet der svaret ikke kan endres.
- `hoopit_set_participation` krever `participation`-rettighet og stabile ID-er for
  person, svar, arrangement og forekomst. Det sender
  `PATCH /app/events/{event}/occurrences/{occurrence}/responses/{response}/`
  (API-versjon 4, vanlige JWT- og App-Version-headere) med `response: 2` ved
  påmelding eller `1` ved avmelding, og valgfri `response_reason` ved avmelding.
- Egen person/barn, alle ID-koblinger, kjent status, svarrettighet, avlysning/lås,
  registrert oppmøte, sluttid og svarfrist kontrolleres før sending. Aktiviteter
  eller svar knyttet til betaling avvises.
- Etter sending leses egne svar på nytt. Kvitteringen har mål-ID-er, tidspunkt og
  faktisk status; venteliste vises som venteliste.

### Svar i innlegg og aktiviteter

Klienten sender `POST /app/posts/{post}/comments/` (versjon 2) og
`/app/events/{event}/occurrences/{occurrence}/comments/` (versjon 1). Begge tar
`comment` og valgfri `parent` for svar under en hovedkommentar. `replied_to` for
dypere svarnivåer brukes ikke; verktøyet godtar bare bekreftede
hovedkommentar-ID-er. `notify_everyone` sendes ikke.

- `hoopit_thread_context` henter fersk tråd, alle kommentarer og svar, publikum og
  svarrettighet. Postens `are_comments_disabled` eller aktivitetens
  `can_create_comment` styrer om svar er mulig. Ingen lest-markering.
- `hoopit_reply_message` krever `messages`-rettighet. Svaret er synlig for
  deltakerne i tråden; verktøyet er ikke privat chat. Innlogget profil er avsender.
  Tråden og alle relevante kommentarer leses på nytt før sending. Ufullstendige
  sider, feil ID-kobling eller stengte kommentarer stopper sendingen.
- Kvittering krever ny kommentar-ID med nøyaktig tekst, avsender og forelder.

Live lesing har verifisert et innlegg med kommentar, publikum og svarrettighet.
Kommentarfunksjonene ligger i `live_hoopit_messages`.

### Chat via Stream: lesing

Klienten henter chattoken med `GET /app/users/current/tokens/` versjon 1;
innloggingen gir `uuid` og `stream_chat_token`. Stream-applikasjonsnøkkelen i
produksjonskonfigurasjonen er offentlig. Ingen API-hemmelighet eller serverrettighet
brukes.

- `hoopit_list_chats` og `hoopit_get_chat` lager en fersk chatøkt i minnet.
  Tokenets bruker-ID må stemme med Hoopits UUID; Stream kontrollerer signaturen.
  Chatnøkkelen går bare til den faste chatverten. Hoopits Firebase-token, andre
  øktfelter og chatnøkkelen eksponeres og arkiveres ikke.
- Lesing bruker bare `GET https://chat.stream-io-api.com/channels` med
  medlemsfilter for innlogget bruker, `type: messaging`, `state: true`,
  `watch: false`, `presence: false` og eventuelt eksakt `cid`, som i
  [Streams kanaloppslag](https://getstream.io/chat/docs/javascript/query-channels/).
  `channel.query`/`watch` brukes ikke ved lesing, fordi de kan opprette kanaler
  ([Stream](https://getstream.io/chat/docs/javascript/creating-channels/)).
  Ingen websocket, lest-markering eller pushmelding startes.
- Listen pagineres med offset, 20 chatter per side, opptil API-grensen 1000.
  Samtidige endringer og nådd grense vises. Listeoppslag har opptil 20 meldinger
  per chat, detaljoppslag opptil 300. Eldre meldinger, trådsvar og vedlegg leses
  med egne verktøy (under).
- Deltakere, `member_count`, eget medlemskap, `own_capabilities`, låseflagg og
  meldingenes kanaltilknytning valideres. Høyst 100 medlemmer returneres, med
  eksplisitt `members_complete`. Foreldreinnsyn kan gå utover deltakerlisten.
- GET 401 kan hente én ny chatøkt. 403, timeout og 429 gjentas ikke automatisk.

Live-verifisert: egne chatter og meldinger med komplett deltakerliste.

### Eldre meldinger og trådsvar

`hoopit_search_chat` bruker Streams `GET /search` med søket som JSON i
query-parameteren `payload`. `hoopit_chat_replies` bruker
`GET /messages/{parent_id}/replies` med `limit` og eventuelt `id_lt`. Ingen
kanalopprettelse, watch, websocket eller lest-markering.

- [Streams søk](https://getstream.io/chat/docs/javascript/search/) støtter kanal- og
  meldingsfiltre, fritekst, tidsfiltre og cursorpaginering. `hoopit_search_chat`
  har valgfri query, inklusiv `since` og eksklusiv `until`, og 20 treff per side.
  Første side sorteres på `created_at` og ID; neste side bruker kildens cursor uten
  offset eller ny sortering.
- `since` og `until` sendes som separate `created_at`-uttrykk under `$and`. To
  operatorer i samme feltuttrykk gir HTTP 400
  (`field expressions can only contain one operation`).
- Sidemarkøren er en lokal UUID bundet til konto, chat og filtre. Den utløper med
  serverens sidetilstand og lagrer ikke meldingsinnhold.
- Søk dekker bare indekserte vanlige meldinger og svar. Systemmeldinger og slettede
  meldinger er ikke i indeksen, så `history_complete` er alltid false, også ved
  `end_of_results`.
- [Kanallisten tar ikke med alle trådsvar](https://getstream.io/chat/docs/javascript/threads/).
  `hoopit_chat_replies` validerer hovedmeldingen og eventuell sidemarkør mot valgt
  CID/parent før GET. Sidene er eldste først innenfor de siste 20 svarene før
  markøren; følg `next_before_id` bakover. ID-grensen er eksklusiv, og like
  tidsstempler beholdes. `thread_complete` gjelder bare når én første side rommer
  alle svar og stemmer med kildens `reply_count`. `end_of_thread` alene beviser
  ikke at tidligere sider er lest.
- Begge bekrefter eget medlemskap gjennom fersk, autentisert kanalliste og
  validerer hver meldings CID. Ingen `read-channel`-kapabilitet kreves; live-listen
  har ikke feltet. `read-events` gjelder lest-kvitteringer, mens API-et håndhever
  lesetilgang. Slettet innhold vises ikke som tekst, selv om kilden returnerer
  rester i tekst- eller vedleggsfelt.
- Listeverktøyene merker vedlegg som ulest og lager ikke oppfølgingskvitteringer.

Live-verifisert: søkepaginering over to sider (også med begge tidsgrenser) og tom
tråd. Ikke-tomme svartråder er bare syntetisk testet.

### Ferske chatmeldinger og vedlegg

`hoopit_get_item` støtter `{kind: chat_message, cid, id}` fra chatoppslag. Det
henter hovedmelding, opptil 1000 trådsvar og 10 siterte meldinger direkte. Eget
medlemskap, kanal, forelder, svarantall og hovedmeldingen etter lesing
kontrolleres. Manglende sitater, uleste svar til sitater, sidegrenser, lyd/video,
avstemninger og lenkeforhåndsvisninger gir eksplisitt ufullstendig grunnlag.

`hoopit_read_attachment` henter ferske signerte adresser ved behov. URL-signaturer
inngår ikke i vedleggs-ID og vises ikke i resultatet. Nedlasting går uten auth og
cookies til et eksakt validert underdomene av `stream-io-cdn.com` eller konfigurerte
vedleggsverter. DNS-, redirect- og størrelsesvern og kildepauser gjelder. PDF-sider
og tekst følger vanlig sidevis lesing og privat opprydding. Kontrakten bygger på
klientens vedleggsmodell og CDN-håndtering og
[Streams vedleggsdokumentasjon](https://getstream.io/chat/docs/javascript/file-uploads/).

Live-verifisert for en chatmelding uten vedlegg eller svar. Nedlasting av chatfiler
er ikke live-verifisert, fordi de undersøkte chattene ikke har vedlegg.
Chatdetaljer ligger i `live_hoopit_chat_details`.

### Mottakervalg for nye samtaler

`hoopit_chat_groups` bruker `GET /app/messaging/contacts/groups/` versjon 1.
`hoopit_chat_recipients` bruker
`GET /app/messaging/contacts/groups/{group_id}/{role}/` med `page` og
`pagination_count=true`, versjon 1. Rollen er `admins`, `guardians` eller
`players`. Gruppe-ID-en må finnes i en fersk liste fra det første endepunktet;
ingen generell personkatalog eller fritekstsøk brukes.

- Mottakerne har stabile referanser med gruppe, rolle, numerisk Hoopit-ID og UUID.
  Bare navn, identiteter, meldingsrettighet og foreldrekontroll vises; e-post,
  telefon, profilbilder og private chat-/enhetsfelter fjernes.
- Mottakere med `can_message=true` kontrolleres i Stream med `GET /users`,
  `filter_conditions.id.$in` (høyst 30 ID-er per kall) og `presence=false`
  ([Streams brukeroppslag](https://getstream.io/docs/platform/users/#querying-users)).
  Stream-feltet `hoopit_id` må stemme med kontaktens numeriske ID.
- Manglende, sperrede eller deaktiverte mottakere og `can_message=false` beholdes
  med `chat_available=false` og årsak; ingen velges bort stille. Egen person er
  ikke mottaker. Feil eller dupliserte identiteter avvises.
- Kontaktsider oppgir totalantall, `next_page`, `end_of_list` og nådd sidegrense
  (20). `coverage_complete` gjelder bare når første side rommer hele rollen i
  gruppen. Samtidige endringer og foreldreinnsyn er oppgitte begrensninger.
- Tilgjengelig mottaker betyr ikke at sending er tillatt; skriveverktøyet
  validerer listen på nytt.

Live-verifisert: kontaktgrupper, alle tre roller, paginering med ufullstendig
dekning, utilgjengelige spillere og chatidentitet for tillatte mottakere.

### Ny tekstchat

`hoopit_send_message(operation_id, recipient_references, content)` krever
`messages`-rettighet. Mottakerreferansene fra kontaktverktøyet valideres på nytt:
paginering, numerisk ID, UUID, `can_message`, Stream-kobling og konto-/
blokkeringsflagg. Avsender kan ikke være mottaker. Duplikater og utilgjengelige
personer avvises samlet; Hoopit-appen utelater ukjente mottakere stille, men det
skal aldri skje her. Første versjon tillater 1–99 mottakere pluss avsender, uten
ekstra medlemsbatcher.

- Én mottaker: et lesende `GET /channels` med medlemsfilter for begge og
  `member_count=2`. Én eksisterende samtale kan gjenbrukes; flere krever at
  brukeren velger CID og bruker svarverktøyet. Finnes ingen, åpnes en
  medlemsbasert samtale med `POST /channels/messaging/query`.
- Flere mottakere: en kanal-UUID avledes fra operasjons-ID-en, kontrolleres som
  ledig og brukes i `POST /channels/messaging/{id}/query`.
- Opprettelsen sender bare `data.members`, `state=true`, `watch=false` og
  `presence=false`. Ingen websocket, navne- eller medlemsendring, automatisk
  sletting eller melding.
- Åpning og melding er separate deloperasjoner. Returens CID og komplette publikum
  må stemme og bekreftes av et ferskt leseoppslag. Ved timeout kontrolleres
  kanal-ID-en, eller en entydig 1:1-samtale med eksakt publikum. Ingen melding
  sendes før åpningen er bekreftet. Deretter valideres mottakerne på nytt, og
  svarverktøyets kvitteringskontroll brukes med bundet avsender og publikum.
- Annen gruppe- eller rollekontekst kan ikke omgå en uavklart identisk melding.
- Blokkeres sendingen etter opprettelsen, kan en tom samtale bli stående.
  Kvitteringen bevarer delresultatet; samtalen slettes ikke automatisk.

Lesende live-forberedelse bekreftet mottakervalg og GET-filteret for 1:1.

### Svar i eksisterende chat

`hoopit_reply_chat` sender tekst i en eksisterende samtale som brukeren peker ut.
Valgfri `parent_id` svarer i tråd under en bekreftet hovedmelding. Krever
`messages`-rettighet. Samtaleopprettelse, medlemsendringer, vedlegg, omtaler,
redigering, sletting og slashkommandoer inngår ikke. Kontrakten er klientens
`POST /channels/{type}/{id}/message` og `GET /messages/{id}`, og
[Streams meldingskontrakt](https://getstream.io/chat/docs/javascript/send-message/).
Vanlige varsler og automatisk lenkeforhåndsvisning beholdes.

- Ferskt kanaloppslag (GET) må bekrefte hele deltakerlisten, eget medlemskap,
  `send-message` eller `send-reply`, ingen blokkering eller frysing og at avsender
  ikke er utestengt. `config.max_message_length` (5000 ved live GET) håndheves i
  tillegg til lokal grense på 20000. Forelder må finnes i samme chat og være en
  uslettet hovedmelding.
- Meldings-ID-en avledes fra operasjonens UUID og lagres før POST. Et direkte
  oppslag må vise at ID-en ikke finnes; en eksisterende melding overskrives aldri.
  Deltakerlisten kontrolleres igjen før sending.
- Forespørselen sender `message: {id, text, attachments: [], mentioned_users: []}`
  og eventuell `parent_id`, med `skip_push: false` og `skip_enrich_url: false`.
  Brukerens eget Stream-token bestemmer avsender, ikke et server-token.
- Kvittering leser eksakt meldings-ID. ID, chat, avsender, tekst, forelder og
  deltakere må stemme. Slettet, skjult eller begrenset synlighet er ikke bekreftet
  levering. Motstridende leverandørkvittering gir `unknown`.

## IST Home

Integrasjonen bruker de samme GraphQL-operasjonene som IST Home-appen, og ISTs
GraphQL-skjema for innloggede brukere. Live leseoppslag bekrefter kontekst, fravær,
samtaleliste, full samtale og `getBlogPost(input: ID!)`.

IST kontrollerer tokenet selv og avviser raskt (målt 10. november 2026, rundt
0,1 sekund): uten token eller med utløpt token 401, med ugyldig signatur 401
«invalid signature», og med ukjent nøkkel-ID 200 med
`{"code":"auth_error", …}`. Et svar med status 200 er altså ikke et bevis på at
tokenet ble godtatt.

### Fravær, nye meldinger og svar

- `ist_home_action_context` bruker `getContext`: egne barn og plasseringer,
  basisverdier i `configs` og JSON-overstyringer i `placement.settings.overrides`.
  De flettes som i appen. Overstyringen kan slå av noe basisoppsettet tillater,
  for eksempel merknader.
- `ist_home_list_absences` bruker `getCalendarItems` med valgt tidsrom og typene
  `PlannedAbsence`/`Absence`. Bare valgt plassering returneres. Kildevarsler i
  `failedCalendarFetches` gir eksplisitt ufullstendig dekning og stopper skriving.
- `ist_home_register_absence` bruker
  `registerAbsence(inputs: [RegisterAbsenceInput!]!)` med `placementId`,
  `startsAt`, `endsAt`, `type: Absence` og valgfri `message`. Tidspunkt krever
  eksplisitt tidssone og sendes som ISO UTC.
  Begge tidspunkt sendes uendret; legg ikke automatisk til en dag i sluttiden.
  Positive perioder opptil 366 dager støttes. Validerer eget barn, plasseringens
  periode, `absenceEnabled`, `readonly`, `disableEnrolmentAbsence` og eventuell
  `submitAbsenceMessageEnabled`. Overlapp avvises; IST håndhever også egne
  tidsgrenser. Kvittering krever en ny `PlannedAbsence` med riktig avsender,
  periode, plassering, type og tekst.
- `ist_home_list_conversations` finner samtaler med `getConversations`. Bruk
  returnert `reference` med `ist_home_get_item` for hele samtalen
  (`getConversation`, høyst 5000 meldinger, 50 per side). Innlegg hentes med
  `getBlogPost`. Gjentatt markør, overlappende sider eller endret
  mottaker-/barnetilknytning under oppslaget avvises. Vedlegg leses med
  `ist_home_read_attachment`, med ferskt kildeoppslag og midlertidige filer.
- `ist_home_reply_message` bruker `messageConversation(input: MessageInput!)` med
  `conversationId`, `content` og tom `attachmentIds`. Krever `messages`-rettighet,
  en gjeldende plassering som tillater meldinger, eget barn, egen deltakelse og
  ulåst samtale. Ingen lest-markering. Gamle samtaler uten gjeldende plassering
  kan fortsatt leses.
- `ist_home_recipients` bruker `getTeacherSearchURL(unitId)` og signert HTTPS GET
  til `api.ist.com` med bearer-token og tjenestens tre signaturfelter. Søk og
  paginering skjer hos IST (`page.lastCursor`/`page.isEnd`); live-verifisert over
  to sider. Ingen mottakerliste lagres.
- `ist_home_send_message` oppretter samtalen med `openConversationEx` (`title`,
  `placementId`, `recipientIds`) og sender deretter `content` og tom
  `attachmentIds` via `messageConversation`. Barn, plassering, mottaker-ID-er,
  `enableMessaging`, `messagingEnableOpenChat`, `readonly` og antallsgrensen
  `conversationTeacherLimit` kontrolleres først; manglende eller ugyldig grense
  avvises. Samtalens kilde-ID lagres før videre oppslag, så tomme samtaler kan
  kontrolleres etter avbrudd selv om de ikke står i samtalelisten. Uten kilde-ID
  avgrenses kontrollen til nye, entydig samsvarende samtaler de siste 14 dagene.
  Før første melding kontrolleres mottakere og barnetilknytning igjen; endringer
  stopper sendingen. Opplasting av vedlegg og avdelinger som mottakere inngår ikke.

### Bestilling og avbestilling av samtaletid

Dette er deltakelse i en invitert foreldresamtale, ikke generell påmelding.
[ISTs veiledning for samtalebooking](https://www.ist.com/kunnskapsbase/samtalebooking)
bekrefter invitasjoner, ledige tider og avbestillingsfrist. Funksjonen må være
slått på hos skolen eller barnehagen; MCP slår den ikke på.

- `ist_home_list_bookings` leser `getCalendarItems` med `types:[TimeBooking]` og
  valgt tidsrom/plassering. Det finnes ingen paginering her; avgrenset dekning og
  `failedCalendarFetches` oppgis.
- `ist_home_booking_context` bruker `getTimeBooking(input:ID!)` med
  kalenderelementets ID. Det gir status, stabil kombinert invitasjons-ID,
  `bookedSlot`, `availableSlots` og plassering. Slottenes ID, bookingtilknytning,
  plassering, tidspunkt og `cancelled` kontrolleres.
- `ist_home_set_participation(..., accepted:true)` bruker
  `allocateTimeSlot(inputs:AllocateTimeSlotInput!)` med
  `combinedBookingAndInvitationId` og valgt `timeSlotId`. Bare en åpen invitasjon
  (`SENT`) uten bestilling endres. Er samme tid allerede booket, gis en
  uendret-kvittering etter ferskt oppslag. En annen bestilling flyttes ikke.
- `accepted:false` bruker `cancelTimeSlot(inputs:[ID!]!)` med **den kombinerte
  invitasjons-ID-en**, ikke kalender-ID eller slot-ID. Status må være `BOOKED`,
  og valgt slot må være den som er booket. `ICBCancelLimit` er antall timer før
  start; kildens nåtid pluss grensen må være strengt før start. Manglende,
  negativ eller ugyldig grense avvises. Appen gjør samme beregning.
- Begge krever `participation`-rettighet, eget barn/plassering,
  `enableTimeBookings=true`, ikke skrivebeskyttet konfigurasjon, gyldig periode og
  ingen rapportert kildesvikt. Kontekst og tidspunkt kontrolleres igjen før én
  mutasjon; IST håndhever også tilgjengelighet og rettigheter.
- `ItemsUpdate`/`ItemsUpserted`/`ItemsDeleted` fra mutasjonen kontrolleres for
  motstridende resultat. Ferskt detaljoppslag må bekrefte samme booking og
  invitasjon, og enten `BOOKED` med eksakt slot/periode eller `CANCELLED` uten
  booket slot. Manglende treff, kildesvikt og andre avlysningsstatuser beviser
  ikke avbestilling. Registeret lagrer ID-er, tidsrom og status; invitasjonstekst
  og tittel arkiveres ikke.

Live: listeoppslaget virket for eget barn, men plasseringen hadde
`enableTimeBookings=false`. Detaljoppslag og skriving er derfor ikke bekreftet
med en aktiv invitasjon.

## Gmail

Målrettet lesekilde med bare `gmail.readonly` og ingen skrivehandlinger; se
[gmail.md](gmail.md). Desktop OAuth er live-verifisert med riktig lesescope og
konto. Målrettede søk er bare testet med simulerte Google-svar.

## Lesedekning og trafikk

Eksisterende API-klienter beholder tokenfornyelse, kildelås per prosess,
trafikkgrenser, paginering og verts-/størrelsesvalidering av vedlegg. Feil i én
kilde gir ikke gamle arkivdata som erstatning og blokkerer ikke andre kilder.
Mellomlagring, ferskhet og det felles trafikkbudsjettet (1 000 forespørsler per
time og 3 000 per døgn per installasjon og tjeneste) står i [read-cache.md](read-cache.md).

Søk er begrenset tekstfiltrering, og ingen historiske endringer loves.
Aktivitetens tidspunkt er ikke meldingens publiseringstid. Varsler uten eget
verifisert detaljendepunkt leses gjennom listeverktøyet.

### Lokal tempobegrensning (`live_traffic`)

Gjelder kildene med `paced=True` i `sources.REGISTRY` (Vigilo, Skolemelding og
IST Home); Spond og Hoopit har egne grenser og pauser. Grensene er lokale, ikke
leverandørens kvoter.

- Minst 250 ms mellom forespørsler og høyst 1000 forespørsler per
  verktøyoperasjon. Kildelåsen dekker både reservering og nettverkskall.
- Siste forespørsel og eventuell pause lagres atomisk i
  `credentials/<kilde>-live-traffic.json`, så omstart og andre MCP-prosesser
  respekterer dem. URL-er, token, innhold og svar lagres ikke der.
- HTTP 429 lagrer `Retry-After` (minst 60 sekunder, 15 minutter når feltet
  mangler). HTTP 503 med `Retry-After` lagrer også pause. Neste kall stopper før
  sending; trafikkbegrensningen gjentar aldri lesing eller skriving. Når pausen er
  ute, kan neste brukerstartede operasjon gå.
- En forespørsel som stoppes før sending, merkes `ApiRequestNotSent`, så
  tokenfornyelsen ikke får en falsk markør for ukjent utfall.
- Gjelder også ISTs GraphQL, signerte mottakersøk, vedlegg og OAuth-transport.

## Ikke implementert

- Skolemelding: svar (ingen stabil avsender-ID) og deltakelse. Direkte føring av
  fravær i skolens register er ikke verifisert og må ikke forveksles med
  fraværsmeldingen.
- IST Home: andre deltakelsesformer enn samtalebooking.
- Spond og Hoopit: fravær. På-/avmelding av aktiviteter skal ikke presenteres som
  fraværsregistrering.
- Uverifiserte handlinger eksponeres ikke, heller ikke via et konfigurasjonsflagg.
- Kommentarverktøyene i Spond og Hoopit erstatter ikke privat melding; bruk
  chatverktøyene.
