# Funksjoner og API-verifikasjon

Den selvstendige kommandolinjen og MCP-serveren tilbyr **bare lesing**. En
eldre profil med skriverettigheter brukes også i lesemodus. Å lese en beskjed
gir ingen rett til å sende, endre eller slette den. Kalender og e-postutsending
krever en annen kobling i assistenten.

De fleste leseverktøyene er `<tjeneste>_list_items`, `<tjeneste>_get_item` og
`<tjeneste>_read_attachment`. Gmail har målrettet søk i stedet for en liste.
`family_connections` viser aktiverte tjenester, lokal innloggingsstatus og
funksjonsstatus. Se [oppsettet](kom-i-gang.md) for innlogging og kjøring.

«Ikke verifisert» betyr at vi mangler tilstrekkelig bevis for API-kontrakten
eller resultatkontrollen. Det betyr ikke at tjenesten mangler funksjonen.
Koblingene er uoffisielle, unntatt Gmail, som bruker Googles offisielle API.

| Tjeneste | Lesing i den selvstendige serveren |
|---|---|
| [Vigilo](#vigilo) | Barn og enheter, meldinger, nyheter, henteavtaler, fravær, møteinvitasjoner, påmeldingsskjemaer og vedlegg |
| [Skolemelding](#skolemelding) | Barn og skoler, innboks og sendtmappe, tråder, lærermottakere og vedlegg |
| [Spond](#spond) | Profil og grupper, innlegg, aktiviteter, kommentarer, varsler, chatter og vedlegg |
| [Hoopit](#hoopit) | Profil og barn, innlegg, aktiviteter, kommentarer, varsler, chatter, kontaktgrupper og vedlegg |
| [IST Home](#ist-home) | Barn og plasseringer, innlegg, samtaler, mottakere, fravær, samtaleinvitasjoner og vedlegg |
| [Gmail](#gmail) | Målrettede e-postsøk, meldinger og vedlegg |

## Vigilo

Leseren bruker API-rutene til Vigilos Android-app. Barn, enheter, mottakere,
tråddeltakere, fraværskoder og fraværsliste er kontrollert med leseoppslag.

`vigilo_list_pickup_notes` henter avtaler for én måned. `dateOccurrences` fra
kilden bestemmer datoene; verktøyet beregner ikke gjentakelser selv. Resultatet
skiller gjeldende, overstyrte, tvetydige og deaktiverte avtaler per dato, enhet
og kategori. Manglende treff beviser ikke at det ikke finnes en avtale. Denne
lesingen er syntetisk testet, men ikke verifisert med et ekte svar.

`vigilo_list_meetings` og `vigilo_list_registrations` leser invitasjoner og egne
svar. Møte-, skjema- og henteavtalereferanser kan leses på nytt med
`vigilo_get_item`. Konkrete møte- og skjemasvar er foreløpig bare kontrakttestet;
leseoppslagene som ble prøvd, ga ingen invitasjoner. Lesing registrerer ingen svar.

## Skolemelding

Barn, skoler og lærermottakere leses med `skolemelding_action_context` og
`skolemelding_recipients`. Motstridende læreropplysninger gir uavklart
tilknytning, ikke en antatt rettighet. Mottakerlisten lover ikke full dekning.

`skolemelding_thread_context` krever eksplisitt trådvalg når en melding har flere
tråder. Full svarpaginering er ikke bekreftet, så oppslaget markeres ufullstendig.
`skolemelding_list_sent_messages` leser sendtmappe sidevis og oppgir begrenset
dekning ved motstridende eller mangelfulle sider. Vedlegg leses separat.

Innlogging, tokenrotasjon og lesing i nye prosesser er prøvd; maksimal
innloggingslevetid er ikke målt. Se [fornyelse](auth-lifecycle.md).

## Spond

Innlegg, aktiviteter og kommentartrær kan leses uten å markere noe som lest.
`spond_list_chats` og `spond_get_chat` leser egne chatter og meldinger sidevis.
Tidsmarkøren kan gi begrenset dekning ved like tidspunkt eller samtidige
endringer; dette oppgis i resultatet. Vedlegg leses separat. Chatlisten og
meldingspagineringen er verifisert med leseoppslag.
Chat-API-ets `maxNum` er inklusiv, og melding nummer null finnes. Leseren
fjerner derfor grensemeldingen fra neste side uten å trekke én fra markøren.

Etter én tokenfornyelse betyr 401 at brukeren må logge inn på nytt. 403 betyr at
ressursen er utilgjengelig, ikke nødvendigvis at hele innloggingen er utløpt.
Når en utilgjengelig ressurs hindrer full lesing, vises `complete=false`.

## Hoopit

Innlegg, aktiviteter og kommentarer leses med avgrenset sideinndeling;
[Hoopit](hoopit-ingestion.md) beskriver grensene. Chatter leses gjennom Stream.
Mottakeroppslag gir bare nødvendige identiteter og viser utilgjengelige
mottakere med årsak. Mottakerlisten er ikke en generell personkatalog.

Chatdetaljer og vedlegg bruker egne leseverktøy. Leseoppslag har bekreftet en
chatmelding uten vedlegg; nedlasting av chatvedlegg er ikke live-verifisert.
Ingen av leseverktøyene sender melding eller endrer deltakelse.

## IST Home

Innlegg og samtaler leses med ISTs GraphQL-API. `ist_home_list_conversations`
finner samtaler; bruk referansen med `ist_home_get_item` for hele samtalen og
`ist_home_read_attachment` for vedlegg. Gjentatte markører, overlappende sider
og endringer i tilknytningen under lesing avvises.

`ist_home_list_absences`, `ist_home_list_bookings` og
`ist_home_booking_context` leser fravær og samtaleinvitasjoner. Begrensninger fra
kilden oppgis i svaret. Bookinglisten er prøvd, men ikke detaljoppslag for en
aktiv invitasjon. Et HTTP 200-svar kan inneholde `auth_error`; det regnes ikke
som vellykket innlogging.

## Gmail

Gmail brukes med `gmail.readonly` og målrettede søk, ikke som en full feed. Se
[Gmail](gmail.md) for søk, begrensninger og oppsett av egen Google-klient.
Desktop-innlogging er prøvd med lesescope; målrettede søk er testet med
simulerte Google-svar.

## Lesedekning og trafikk

`complete=true` gjelder det avgrensede oppslaget, ikke all historikk. Feil,
sidegrenser eller manglende dekning vises i resultatet. Søk filtrerer bare det
aktuelle oppslaget. Manglende treff beviser ikke at en beskjed er slettet eller
et arrangement avlyst.

Kildelås, tokenfornyelse, paginering og validering av vedlegg gjelder fortsatt.
Mellomlagring, ferskhet og lokale trafikkgrenser står i
[mellomlagring](read-cache.md). Grensene er lokale vern, ikke leverandørens
kvoter eller en garanti mot utestenging.

## Intern bibliotekskode for skrivehandlinger

Pakken inneholder også intern kode for enkelte skrivehandlinger. Den er ikke et
støttet grensesnitt i den selvstendige CLI-en eller MCP-serveren. Tabellens
«implementert» beskriver koden, ikke en funksjon brukeren kan slå på her.
Python-modulene er interne, se [veien til 1.0](release.md).

| Tjeneste | Intern implementasjon | Ikke verifisert |
|---|---|---|
| Vigilo | Nye meldinger og svar, heldagsfravær, møter og påmeldingsskjemaer | — |
| Skolemelding | Ny melding til én lærer og fraværsmelding | Svar og deltakelse; fraværsmeldingen fører ikke fravær i skolens register |
| Spond | Nye chatter, chatsvar, kommentarsvar og deltakelsessvar | Fravær |
| Hoopit | Nye chatter, chatsvar, kommentarsvar og deltakelsessvar | Fravær |
| IST Home | Nye samtaler og svar, fravær, booking og avbestilling av samtaletid | Andre deltakelsesformer; bookingdetaljer med aktiv invitasjon |
| Gmail | Ingen skrivekode | — |

Skriving er bare testet mot syntetiske mål. Koden krever verifisert
API-kontrakt, eksplisitt kategori (`messages`, `absence` eller `participation`)
og en konkret beskjed fra brukeren. Tekst fra kilden er ikke autorisasjon.
`live_actions.py` og `sources.REGISTRY` registrerer bare implementerte og
verifiserte handlinger.

Ingen tjeneste har en verifisert idempotensnøkkel. `action_ledger.py` binder
operasjons-ID og innholdets fingeravtrykk før sending. Timeout, avbrudd eller
uklar kvittering gir `unknown`; samme operasjon utfører bare kontrolloppslag,
og en identisk uavklart handling kan ikke sendes på nytt med ny ID. Kontroll
gjøres med fersk kildeinformasjon, ikke cache. En vellykket HTTP-retur alene
beviser ikke levering. Registeret lagrer ikke meldingstekst eller merknader.

Kontraktene og avgrensningene for hvert kall ligger i `live_vigilo*.py`,
`live_skolemelding*.py`, `live_spond*.py`, `live_hoopit*.py` og `live_ist*.py`.
Kontroller verktøyets tilgang, avsender, mål og oppfølging i koden før endring.

- Vigilos `singleChoice` og `multipleChoices` bruker kildens `choiceNumber` som
  rang; bare `rankedChoices` bruker brukerens rekkefølge. Manglende invitasjon
  er ikke bevis på avmelding.
- Skolemeldings svar er ikke verifisert med en stabil avsender-ID; en
  fraværsmelding er ikke det samme som føring av fravær. Oppfølging av sending
  krever én ny, entydig melding i sendtmappen, ikke bare et POST-svar.
- Spond- og Hoopit-kommentarer er synlige i tråden og er ikke private
  chatmeldinger. Ny Hoopit-chat kan opprette en tom samtale før sendingen
  eventuelt stoppes; den slettes ikke automatisk.
- ISTs booking gjelder bare inviterte samtaletider. Avbestilling bruker den
  kombinerte booking-/invitasjons-ID-en, ikke kalender- eller slot-ID, og krever
  en gyldig avbestillingsfrist og en fersk bekreftelse.
