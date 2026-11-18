# Prompter som virker

Her er promptene fra [README-en](../README.md) og noen flere. Lim dem inn i
Claude, ChatGPT eller Codex når familiekilder-mcp er koblet til, og tilpass dem
til familien din.

Serveren kan bare lese. Assistenten kan foreslå et svar, men du sender det
selv i appen. Det assistenten leser, sendes til AI-leverandøren du bruker.

## Del dine egne

Har du en prompt som virker? Del den under **Discussions** på prosjektets
reposide, i kategorien **Show and tell**. Skriv hvilken assistent du bruker,
hvilke tjenester den leser fra, og hva som fungerte. Du kan også sende en pull
request som legger prompten til på denne siden.

Ikke del ekte beskjeder, navn på barn, skoler eller lag, eller skjermbilder med
personopplysninger. Skriv om eksemplene først.

## Oppsett

Installer på Macen. Prosjektet krever macOS, Python 3.11 eller nyere og
[Brave](https://brave.com/).

> Installer familiekilder-mcp fra https://github.com/Item4747/familiekilder-mcp i ~/familiekilder-mcp. Prosjektet krever macOS, Python 3.11+ og Brave. Kjør scripts/setup i bakgrunnen. Veiviseren åpnes automatisk. Innlogging og resten av oppsettet gjør jeg selv der.

Egen server på Fly.io, etter at oppsettet på Macen er ferdig. Se
[veiledningen for egen server](alltid-pa.md).

> Sett opp min egen familiekilder-server på Fly.io. Følg docs/alltid-pa.md i ~/familiekilder-mcp. Installer flyctl, og la meg logge inn hos Fly selv. Kjør så `familiekilder-mcp deploy fly --move`. Gi meg adressen til serveren og stien til filen med eierkoden. Ikke les eller vis eierkoden.

Sjekk at innloggingene virker. Assistenten bruker `family_connections` og
prøver deretter å lese fra hver tjeneste.

> Sjekk hvilke tjenester som er koblet til. Prøv å lese de nyeste beskjedene
> fra hver av dem, og si fra hvis en innlogging må fornyes.

## Kalender og påminnelser

Disse promptene trenger også en kalenderkobling i assistenten som kan opprette
hendelser. familiekilder-mcp skriver ikke i kalenderen selv.

> Gå gjennom alt fra skole, barnehage og fritid. Legg det som skjer, i
> familiekalenderen. Legg det vi må gjøre, inn som egne oppføringer med varsel
> kvelden før: ting som skal være med, skjemaer som skal signeres og påmeldinger
> med frist. Sjekk hva som står der fra før, så ingenting kommer inn to ganger.

Legg til denne teksten når du lager en planlagt oppgave i Codex eller Claude
Desktop. Macen må være på når oppgaven kjører, eller oppgaven må
[kjøre i skyen](alltid-pa.md#en-kveldsoppgave-i-skyen) mot egen server.

> Gjør dette hver kveld kl. 19. Oppdater oppføringer når tid eller sted er
> endret, og slett bare noe når en beskjed sier at det er avlyst. Legg inn en
> oppføring i dag hvis en innlogging har utløpt.

## Oversikt over uka

Datofilteret i `*_list_items` gjelder når beskjeden ble skrevet eller endret,
ikke når aktiviteten er. Be derfor om noen uker tilbake i tid.

> Les beskjeder og aktiviteter fra de siste fire ukene i alle tjenestene. Lag en
> oversikt over det som skjer denne uka og neste, dag for dag, med tid, sted og
> hva barna må ha med. Si fra hvis en tjeneste ikke kunne leses helt.

> Finn alle frister i beskjedene fra de siste tre ukene: påmeldinger, skjemaer
> som skal signeres, betaling og ting som skal leveres. Sorter dem etter dato,
> og si hvilken tjeneste og beskjed hver frist står i.

## Vedlegg

Vedlegg leses med `*_read_attachment`, også PDF-er over flere sider.

> Finn den nyeste beskjeden fra barnehagen med vedlegg. Les vedlegget og fortell
> kort hva det handler om, og om vi må gjøre noe.

## Chat i idrettslaget

Spond og Hoopit har chatter i tillegg til innlegg og aktiviteter. De leses med
`spond_list_chats`, `spond_get_chat`, `hoopit_list_chats` og
`hoopit_get_chat`.

> Oppsummer det som er skrevet i chattene i Spond og Hoopit den siste uka. Hva er
> avtalt, og er det noe vi bør svare på?

## Fravær

Vigilo og IST Home har egne verktøy for registrert fravær:
`vigilo_list_absences` og `ist_home_list_absences`.

> Hvilket fravær er registrert for barna i Vigilo og IST Home siden skolestart?
> Sorter det etter dato.

## E-post sammen med de andre tjenestene

Gmail leses med målrettede søk (`gmail_search`), ikke som en full innboks. Se
[Gmail](gmail.md) for oppsettet.

> Finn e-poster fra fotballklubben om cupen i helgen, og sammenlign med det som
> står i Spond. Si fra hvis tid, sted eller oppmøte ikke stemmer.

## Ditt språk

Assistenten kan forklare beskjeder på språket du foretrekker eller skrive dem
om til enklere norsk.

> Les de nye beskjedene fra skolen. Forklar på polsk hva vi må gjøre
> og hvilke frister som gjelder. Foreslå et kort svar på norsk.

> Skriv beskjeden fra barnehagen i dag om til enkel norsk med korte setninger.
> Lag en liste over det vi må huske.
