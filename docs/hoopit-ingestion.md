# Hoopit: token og API

Hoopit kobles til med et personlig tilgangstoken og leses gjennom
[Hoopits API](https://api.hoopit.io/external/docs/). Tokenet virker som deg: det
gir tilgang til det du og barna dine kan se i Hoopit-appen, innenfor tilgangene
du krysser av for. Koden står i `hoopit_auth.py` (tokenet), `hoopit.py` (kallene),
`live_hoopit_reader.py` (lister og enkeltelementer) og `live_hoopit.py`
(kommentarer og svar på aktiviteter).

## Slik lager du tokenet

1. Åpne [tokensiden hos Hoopit](https://id.hoopit.io/security/access-tokens/)
   og logg inn som vanlig.
2. Skriv for eksempel «familiekilder» i «Navn».
3. Under «Dine» krysser du av for disse tre:
   - «Se aktivitetene og invitasjonene dine, og barnas»
   - «Svare på invitasjoner for deg selv og barna dine»
   - «Se innlegg og kommentarer i gruppene dine»

   Boksene under «Som administrator» trengs ikke.
4. Velg hvor lenge tokenet skal virke i «Utløper om» (Hoopit foreslår 1 år).
   Når det har utløpt, lager du et nytt på samme måte.
5. Trykk «Opprett et token», og kopier tokenet. Hoopit viser det bare én gang.
   Det begynner med `hoopit_pat_`.
6. Lim det inn i veiviseren (`familiekilder-mcp setup`, «Koble til med token»),
   eller kjør

   ```sh
   familiekilder-mcp configure hoopit
   familiekilder-mcp auth hoopit token
   ```

   og lim inn tokenet når du blir spurt. Det vises ikke mens du limer. Fra en fil
   eller et annet program: `familiekilder-mcp auth hoopit token --stdin < fil`.
   Skriv aldri tokenet som et argument; da havner det i kommandohistorikken.

Programmet kontrollerer først at teksten ser ut som et Hoopit-token. Så leser det
én side innlegg og én side aktiviteter med tokenet. Først når begge lykkes, lagres
tokenet privat i `.private/credentials/hoopit-session.json` (bare lesbart for
deg). Feilmeldingene sier hva som er galt:

| Melding | Hva du gjør |
|---|---|
| Tokenet er ikke helt / ikke et Hoopit-token | Kopier hele tokenet på nytt fra Hoopit |
| Hoopit avviste tokenet. Det er utløpt eller trukket tilbake | Lag et nytt token |
| Tokenet mangler «Se innlegg og kommentarer i gruppene dine» (eller en annen boks) | Lag et nytt token med alle tre boksene under «Dine» avkrysset |

«Svare på invitasjoner for deg selv og barna dine» kan ikke kontrolleres uten å
svare på noe. Mangler den, sier svaret på en aktivitet fra det; lesingen virker
som før.

Hoopits tokenside viser bare boksene, ikke navnene API-et bruker. Sammenhengen,
for den som leser koden eller API-dokumentasjonen:

| Boks under «Dine» | Tilgang i API-et |
|---|---|
| Se aktivitetene og invitasjonene dine, og barnas | `events:read` |
| Svare på invitasjoner for deg selv og barna dine | `events:respond` |
| Se innlegg og kommentarer i gruppene dine | `posts:read` |

Bytt token med `familiekilder-mcp auth hoopit token --replace-existing`, eller
«Bytt token» i veiviseren. `familiekilder-mcp auth hoopit logout` sletter tokenet
her; det virker hos Hoopit til du sletter det på tokensiden.

## Når tokenet slutter å virke

Svarer Hoopit 401 under lesing, merkes tokenet som avvist, og statusen
(`auth hoopit status`, `family_connections`) sier at det er utløpt eller trukket
tilbake og hvordan du lager et nytt. Svarer Hoopit 403 på listen over innlegg
eller aktiviteter, sier statusen hvilken tilgang som mangler. Begge deler står til
du kobler til et nytt token. Programmet prøver ikke på nytt i mellomtiden.

En 403 på kommentarene til ett innlegg gjelder bare det innlegget: innlegget
vises uten kommentarer og med `content_complete=false`, og tokenet beholdes. En
403 på et svar på en aktivitet gir avslag på det svaret (tokenet kan mangle
«Svare på invitasjoner for deg selv og barna dine»), uten å stoppe lesingen.

Kan det lagrede tokenet ikke leses (for eksempel fordi filen er endret for
hånd), sier statusen det. Koble til på nytt og velg å erstatte tokenet, eller
logg ut; begge deler fjerner den uleselige filen.

## Hva som leses

| Kall | Brukes til |
|---|---|
| `GET me/posts/` | Innlegg du eller barna kan lese, nyeste først |
| `GET posts/{id}/comments/` | Kommentarer og svar på ett innlegg, eldste først |
| `GET me/occurrences/?from_date&to_date` | Aktiviteter i et tidsrom, med familiens svar |
| `PATCH events/{id}/occurrences/{id}/responses/{person}/` | Svar på en aktivitet (bare med skriverettigheten `participation`) |

`hoopit_list_items` leser innlegg fra de siste 14 dagene, eller fra `since`.
Innlegg som er opptil 14 dager eldre, får kommentarene sine lest også, fordi en ny
kommentar på et eldre innlegg er nytt innhold. Sidene leses til `has_next=false`,
men stopper etter en side der alle innleggene er eldre enn dette. Aktivitetene
leses fra starten av tidsrommet til 120 dager fram. Hoopit svarer 400 når
`from_date` og `to_date` ligger mer enn 366 dager fra hverandre, så et lengre
tidsrom leses i deler på høyst 365 dager.

Når en grense nås (sider, innlegg, aktiviteter eller forespørsler), gir listen
det som er lest, med `complete=false`, og grensen står i loggen. Lesingen feiler
ikke av den grunn.

API-et har ikke et eget kall for ett innlegg eller én aktivitet for vanlige
medlemmer. `hoopit_get_item` finner derfor innlegget i listen og aktiviteten i
aktivitetslisten. Referansene har med dagen (`day`): opprettelsesdagen for et
innlegg og dagen for en aktivitet. Innleggsoppslaget stopper når listen kommer
til innlegg som er eldre, og aktivitetsoppslaget holder seg til noen få dager.
Finnes elementet ikke, svarer oppslaget at det ikke ble funnet.

API-et oppgir ikke når et innlegg eller en aktivitet sist ble endret. For innlegg
er endringstiden den nyeste av publiseringen og kommentarene. For aktiviteter
husker programmet hvilken versjon det har sett
(`hoopit-occurrence-versions.json`, bare ID-er, hasher og tidspunkt), og
`changed_at` er når denne versjonen ble sett første gang. En aktivitet som ikke
er endret, beholder tidspunktet.

Hver kildereferanse har bare ID-er og dagen. Kilde-ID-en er `post:<id>` og
`occurrence:<aktivitet>:<forekomst>`, og lenken går til Hoopit-appen
(`https://app.hoopit.io/app/posts/ID`).

## Versjoner

Hvert kall i Hoopits API har sin egen versjon. Programmet ber om en fast versjon
med headeren `Version` (`ENDPOINTS` i `hoopit.py`; i dag versjon 1 for alle fire).
Svarer Hoopit med `Deprecated-Version` eller `Invalid-Version`, lagres det i
`hoopit-api-version.json`, skrives en advarsel i loggen, og statusen viser
`api_version_warning` til Hoopit slutter å sende headeren. En utfaset versjon
fjernes etter omtrent tre måneder, så oppdater programmet når advarselen kommer.

## Trafikk

Kallene går ett om gangen, med minst ett sekunds mellomrom. Hoopit tillater 5 000
kall i timen per token; ett i sekundet holder seg under det. Svarer Hoopit 429,
lagres `Retry-After`, og lesingen stopper uten nytt forsøk. Nettverksfeil og
serverfeil gir også en lagret pause. Én lesing har høyst 300 kall, 20 sider per
liste og 250 innlegg. Pakkens felles trafikkbudsjett i
[mellomlagring](read-cache.md) gjelder i tillegg.

Gjentatte sider, tomme sider med `has_next=true` og grenser som nås, gir aldri en
melding om full dekning.

## Det Hoopits API ikke har

Disse funksjonene er ikke med, fordi API-et ikke har dem:

- chatter (Stream), og å sende eller svare i chat
- vedlegg og bilder på innlegg
- varsler
- kontaktgrupper og mottakere
- å skrive kommentarer
- profil og liste over barn
- beskrivelse, svarfrist, betaling og kommentarer på aktiviteter
- når et innlegg eller en aktivitet sist ble endret

## Kontobinding

API-et viser ikke hvem tokenet tilhører. Programmet gir derfor tokenøkten en
tilfeldig kontobinding første gang du kobler til. Mellomlagrede oppslag hører
til denne bindingen. Bytter du token, beholdes den; logger du ut, starter neste
token en ny. Vil du bytte til en annen Hoopit-konto, logg ut først.

## Prøv tokenet uten å lagre det

`scripts/hoopit_smoke.py` leser med et token mot Hoopit uten å bruke eller endre
datamappen din. Det gjør bare GET-kall og skriver ut antall, ID-er, titler og
tidspunkt, aldri tokenet, tekster eller navn:

```sh
python -I scripts/hoopit_smoke.py --token-file sti/til/tokenfil --posts 5 --days 14
```

Kjør det fra `familiekilder`-mappen med et Python-miljø der pakken er installert.
Tokenet kan også sendes på stdin.

Svar på aktiviteter står i [funksjonsmatrisen](service-capabilities.md#hoopit).
