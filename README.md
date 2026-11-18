# familiekilder-mcp

Koble AI-assistenten din til beskjeder, aktiviteter og vedlegg fra skole,
barnehage og fritid. Programmet er en **MCP-server**: en kobling som lar
assistenten lese fra tjenestene du bruker, med din egen innlogging.

![Illustrasjon: Beskjeder fra barnehage i Vigilo, skole i Skolemelding og fotball i Spond går til assistenten. Hver kveld kl. 19 legger den oppgaver og aktiviteter i familiekalenderen, og telefonen varsler kvelden før.](docs/bilder/fra-beskjeder-til-paminnelser.webp)

*Illustrasjon med oppdiktede data. Assistenten må ha en planlagt oppgave og en
egen kalenderkobling.*

<!-- Tjenestelisten er hentet fra sources.REGISTRY. -->
Støtter Vigilo, Skolemelding, Spond, Hoopit, IST Home, Flyt (barnehage) og Gmail.
Vi jobber med støtte for MyKid og for skole og SFO i Flyt.

Savner du en tjeneste? Opprett en sak på GitHub med navnet på tjenesten,
eller send en pull request hvis du vil bidra med en kobling.

## Serveren er din

Du kjører serveren selv: på din egen datamaskin, eller på din egen konto hos
[Fly.io](https://fly.io/). Ingen data går gjennom eller lagres hos oss som
lager programmet. Vi har ingen server, og programmet sender oss ingenting.

| | På datamaskinen | Din server på Fly.io |
| --- | :---: | :---: |
| Claude og ChatGPT installert på datamaskinen | ✅ | ✅ |
| Claude og ChatGPT i nettleseren | ❌ | ✅ |
| Claude og ChatGPT på mobilen | ❌ | ✅ |
| Virker når datamaskinen er av | ❌ | ✅ |
| Gratis | ✅ | ❌ |
| Innloggingene blir på datamaskinen | ✅ | ❌ |
| Prøvd med alle tjenestene | ✅ Mac med Brave | ❌ |

- **Pris.** Fly.io gir nye kontoer 1 dollar å prøve for i sju dager, uten
  betalingskort. Etterpå må du legge inn kort. Serveren sover når ingen bruker
  den, og koster under 30 kr i måneden. Fly sletter i dag regninger under
  5 dollar, men det kan de endre.
- **Datamaskinen.** Den kan være en Mac, en Windows-PC eller en Linux-maskin
  med skrivebord. Windows og Linux er nytt; se under.
- **Innlogginger.** Du logger alltid inn på datamaskinen. På Fly.io flytter du
  innloggingene til serveren din etterpå.
- **Prøveordning.** Oppsettet på Fly.io er nytt. Spond og Hoopit er prøvd
  der. Tjenestene med BankID-innlogging og Gmail er ikke prøvd ennå.
- **ChatGPT.** I nettleseren og på mobilen trenger du et abonnement som
  tillater egne MCP-servere. Det er prøvd med Pro.

Uansett hvor serveren kjører, kan den bare lese. Det assistenten leser, sendes
til AI-leverandøren du bruker. Gmail bruker Googles offisielle API. De øvrige
koblingene er uoffisielle.

## Kom i gang på datamaskinen

Dette er en lanseringskandidat for versjon 1.0. Den kan brukes på macOS,
Windows og Linux; se under hva som er prøvd. Du trenger Python 3.11 eller nyere og en av nettleserne
[Brave](https://brave.com/), [Google Chrome](https://www.google.com/chrome/)
eller [Microsoft Edge](https://www.microsoft.com/edge) for å logge inn på
tjenestene.

- **Prøvd:** Mac med Brave, med ekte innlogging hos tjenestene.
- **Nytt:** Windows, Linux, og Chrome og Edge på Mac. Installasjonen,
  veiviseren, serveren og innloggingsløpet er prøvd maskinelt mot oppdiktede,
  lokale sider, men ikke med ekte innlogging ennå. Vil du prøve, les
  [Prøve på Windows eller Linux](docs/prove-pa-windows-og-linux.md), og si
  gjerne fra under **Issues**, også når det virker.
- **Windows:** Installer nettleseren for alle brukere på maskinen. En
  nettleser som bare er installert for deg, blir avvist. Edge følger med
  Windows og brukes da i stedet.
- **Linux:** Bruk leverandørens egen .deb- eller .rpm-pakke, og logg inn fra
  skrivebordet, ikke over SSH. Snap, Flatpak og Chromium støttes ikke.

Detaljer står i [oppsettsveiledningen](docs/kom-i-gang.md#nettleseren).

Få kodeagenten til å installere for deg. Åpne Codex i ChatGPT-appen eller Claude og lim inn prompten under.

> Installer familiekilder-mcp fra https://github.com/familiekilder/familiekilder-mcp i ~/familiekilder-mcp. Prosjektet krever Python 3.11+ og Brave, Google Chrome eller Microsoft Edge, på macOS, Windows eller Linux. Kjør scripts/setup.py med Python i bakgrunnen. Veiviseren åpnes automatisk. Innlogging og resten av oppsettet gjør jeg selv der.

Eller installer selv ved å følge [oppsettsveiledningen](docs/kom-i-gang.md).

Bruk serveren fra [Claude Desktop](https://claude.com/download) eller
[ChatGPT-appen for Mac](https://chatgpt.com/download/) på samme maskin. I
ChatGPT velger du **Codex** og **This computer**. Codex i CLI eller IDE kan
også koble til.

## Kom i gang på Fly.io

Gjør først oppsettet på datamaskinen, og logg inn på tjenestene der. Lim så inn
denne prompten i kodeagenten:

> Sett opp min egen familiekilder-server på Fly.io. Følg docs/alltid-pa.md i ~/familiekilder-mcp. Installer flyctl, og la meg logge inn hos Fly selv. Kjør så `familiekilder-mcp deploy fly --move`. Gi meg adressen til serveren og stien til filen med eierkoden. Ikke les eller vis eierkoden.

Når agenten er ferdig:

1. Åpne filen med eierkoden. Lagre koden i passordbehandleren, og slett filen.
2. Legg til adressen i Claude på nett (**Customize > Connectors > Add custom
   connector**) eller i ChatGPT på nett (**Plugins > + > Add custom MCP
   server**).
3. Skriv eierkoden når serveren ber om den. Da virker koblingen også i
   mobilappen.

Eller sett opp selv ved å følge [veiledningen for egen server](docs/alltid-pa.md).

## Slipp å huske alt selv

Koble assistenten også til kalenderen din. Da kan den gå gjennom alt fra
skole, barnehage og fritid og legge det inn i familiekalenderen. Det dere må
gjøre, får egne oppføringer med varsel kvelden før, så telefonen minner dere
på det. Bruker dere en delt kalender, får begge foreldrene det samme.
Assistenten trenger en egen kobling som gir den tilgang til å opprette
kalenderhendelser.

> Gå gjennom alt fra skole, barnehage og fritid. Legg det som skjer, i
> familiekalenderen. Legg det vi må gjøre, inn som egne oppføringer med varsel
> kvelden før: ting som skal være med, skjemaer som skal signeres og påmeldinger
> med frist. Sjekk hva som står der fra før, så ingenting kommer inn to ganger.

## La assistenten passe på hver dag

Codex og Claude Desktop kan kjøre en oppgave fast hver dag. Lag en planlagt
oppgave med prompten over, og legg til teksten under. Da holder assistenten
kalenderen oppdatert uten at du må be om det. Kjøring om kvelden gir tid til å
vaske gymtøy eller kjøpe gave før neste dag. Datamaskinen må være på når
oppgaven kjører. Med egen server på Fly.io kan oppgaven
[kjøre i skyen](docs/alltid-pa.md#en-kveldsoppgave-i-skyen) i stedet, også når
datamaskinen er av.

> Gjør dette hver kveld kl. 19. Oppdater oppføringer når tid eller sted er
> endret, og slett bare noe når en beskjed sier at det er avlyst. Legg inn en
> oppføring i dag hvis en innlogging har utløpt.

## Forstå beskjedene på ditt språk

Assistenten kan forklare beskjeder på språket du foretrekker eller skrive
dem om til enklere norsk. Det gjør det lettere for flere foreldre å få med
seg hva som skjer, følge opp frister og svare på beskjeder.

> Les de nye beskjedene fra skolen. Forklar på polsk hva vi må gjøre
> og hvilke frister som gjelder. Foreslå et kort svar på norsk.

![Illustrasjon: En skolebeskjed ber om signert samtykke til turen innen fredag. Assistenten forklarer beskjeden på polsk og foreslår et kort svar på norsk.](docs/bilder/beskjeder-pa-polsk.webp)

*Illustrasjon med oppdiktede data. Svaret er et forslag du selv kan sende.*

## Flere prompter

[Prompter som virker](docs/oppskrifter.md) samler promptene over og noen
flere. Del dine egne under **Discussions** på reposiden, i kategorien
**Show and tell**, eller send en pull request.

## Leter du etter et Spond API eller Vigilo API?

Så vidt vi vet, har verken Spond, Vigilo, Hoopit, Skolemelding, IST Home eller
Flyt et åpent API for foreldre. Søker du etter Spond API, Vigilo API, Hoopit
API, Skolemelding API, IST Home API eller Flyt API, er dette en uoffisiell
kobling i stedet. Den logger inn med din egen konto og leser beskjeder,
aktiviteter og vedlegg du selv har tilgang til. Den kan ikke sende, svare eller
melde på.

Du bruker den som MCP-server fra Claude, ChatGPT eller Codex. Python-modulene
er interne og ikke et stabilt bibliotek. Oppsettet krever macOS, Windows
eller Linux og en av nettleserne Brave, Chrome eller Edge, som beskrevet over.
Endrer en tjeneste appen sin, kan koblingen slutte å virke til den er rettet.

[Hva tjenestene støtter](docs/service-capabilities.md) ·
[Prompter](docs/oppskrifter.md) ·
[Feilsøking og bidrag](docs/kom-i-gang.md#hjelp-og-bidrag) ·
[Apache-2.0](LICENSE)
