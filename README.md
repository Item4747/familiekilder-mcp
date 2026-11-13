# familiekilder-mcp

Koble AI-assistenten din til beskjeder, aktiviteter og vedlegg fra skole,
barnehage og fritid. Programmet er en **MCP-server**: en kobling som lar
assistenten lese fra tjenestene du bruker, med din egen innlogging.

<!-- Tjenestelisten er hentet fra sources.REGISTRY. -->
Støtter Vigilo, Skolemelding, Spond, Hoopit, IST Home og Gmail.

## Forstå beskjedene på ditt språk

Har du et annet førstespråk enn norsk? Assistenten kan forklare innholdet på
språket ditt, gjøre det tydelig hva du skal gjøre og hjelpe deg å svare på norsk.
Du kan for eksempel be om:

> Les de nye beskjedene fra skolen. Forklar på polsk hva vi må gjøre,
> og hvilke frister som gjelder. Foreslå et kort svar på norsk.

Du kan også be om enklere norsk. Det er AI-assistenten du velger som oversetter
og forklarer, med tilgang til de opprinnelige beskjedene.

## Kom i gang

Dette er en lanseringskandidat for 1.0. Du trenger en Mac, Python 3.11 eller
nyere og en lokal AI-klient: Claude Desktop eller lokal Codex-kjøring i ChatGPT-appen, CLI eller IDE.
Vanlig nett- og mobilchat kan ikke koble til direkte.

Etter at du har lastet ned kildekoden, åpner du Terminal i prosjektmappen og kjører:

```sh
scripts/setup
```

Da åpnes et lokalt oppsett i nettleseren. Legg til tjenestene, logg inn og koble
til AI-assistenten. Noen tjenester trenger en egen oppsettsfil som ikke følger
med. Du kan også [la kodeagenten gjøre installasjonen](docs/agentoppsett.md),
eller følge [oppsettsveiledningen](docs/kom-i-gang.md).

MCP-serveren tilbyr bare lesing. Innloggingene lagres lokalt. Innhold
assistenten leser, kan sendes til AI-leverandøren du bruker. Koblingene er
uoffisielle; Gmail bruker Googles offisielle API.

[Hva tjenestene støtter](docs/service-capabilities.md) ·
[Feilsøking og bidrag](docs/kom-i-gang.md#hjelp-og-bidrag) ·
[Apache-2.0](LICENSE)
