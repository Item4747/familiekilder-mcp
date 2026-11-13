# familiekilder-mcp

Koble AI-assistenten din til beskjeder, aktiviteter og vedlegg fra skole,
barnehage og fritid. Programmet er en **MCP-server**: en kobling som lar
assistenten lese fra tjenestene du bruker, med din egen innlogging.

<!-- Tjenestelisten er hentet fra sources.REGISTRY. -->
Støtter Vigilo, Skolemelding, Spond, Hoopit, IST Home og Gmail.
Vi jobber med støtte for MyKid og Flyt.

Savner du en tjeneste? Opprett en sak på GitHub med navnet på tjenesten,
eller send en pull request hvis du vil bidra med en kobling.

## Forstå beskjedene på ditt språk

Assistenten kan forklare beskjeder på språket du foretrekker eller skrive
dem om til enklere norsk. Det gjør det lettere for flere foreldre å få med
seg hva som skjer, følge opp frister og svare på beskjeder.

> Les de nye beskjedene fra skolen. Forklar på polsk hva vi må gjøre
> og hvilke frister som gjelder. Foreslå et kort svar på norsk.

## Få det viktigste i kalenderen eller på e-post

Koble assistenten også til kalenderen eller e-posten din. Da kan du be den
legge inn foreldremøter, kamper og påmeldingsfrister som gjelder familien din,
eller sende deg en oppsummering på e-post. Assistenten trenger en egen
kobling som gir den tilgang til å opprette kalenderhendelser eller sende e-post.

> Les ukas beskjeder. Legg aktiviteter og frister som gjelder oss, i kalenderen.
> Send meg en kort e-post med det vi må huske å ta med eller gjøre.

## Hvor fungerer det?

Du installerer MCP-serveren på Macen din og bruker den fra
[Claude Desktop](https://claude.com/download) eller
[ChatGPT-appen for Mac](https://chatgpt.com/download/) på samme maskin.
I ChatGPT velger du **Codex** og **This computer**. Lokal Codex-kjøring i CLI
eller IDE kan også koble til. Vanlig chat i nettleseren eller på mobilen kan
ikke koble seg direkte til MCP-serveren.

## Kom i gang

Dette er en lanseringskandidat for versjon 1.0. Du trenger Python 3.11 eller
nyere og [Brave](https://brave.com/) for å logge inn på tjenestene.

Last ned kildekoden, åpne Terminal i prosjektmappen og kjør:

```sh
scripts/setup
```

Veiviseren åpnes i nettleseren. Velg tjenestene du bruker, logg inn og koble til
AI-assistenten. Noen tjenester krever en egen oppsettsfil.

Du kan også [be kodeagenten om å installere for deg](docs/agentoppsett.md) eller
følge [oppsettsveiledningen](docs/kom-i-gang.md).

MCP-serveren tilbyr bare lesing. Innloggingene lagres lokalt. Innhold
assistenten leser, kan sendes til AI-leverandøren du bruker. Gmail bruker
Googles offisielle API. De øvrige koblingene er uoffisielle.

[Hva tjenestene støtter](docs/service-capabilities.md) ·
[Feilsøking og bidrag](docs/kom-i-gang.md#hjelp-og-bidrag) ·
[Apache-2.0](LICENSE)
