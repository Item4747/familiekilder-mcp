# Hoopit: direkte oppslag og sideinndeling

Lesing skjer ved MCP-kall, uten egen innhenting, råfiler eller arkiv.

Postfeeden bruker API-versjon 5 og `pagination_count=false`. Feltet er et boolsk
flagg, ikke en sidestørrelse. Kilde: [Hoopits nettklient](https://app.hoopit.io/main.dart.js)
og ekte sider, kontrollert i september 2026 (klientens SHA-256 da:
`1dbec0027cd400d38ee9d63ba4e7013904dfbef5e8bc66c718c3c3dec05ca112`).

Listeverktøyet traverserer opptil 20 sider per hovedfeed. Det felles budsjettet
på 80 forespørsler inkluderer autentisering, kommentarer og vedlegg. Kall er
serielle per tjeneste, med minst to sekunder mellom dem og varige trafikkpauser
ved 429, nettverksfeil og serverfeil. Nye MCP-prosesser respekterer samme tilstand.

Gamle sider beviser ikke at senere sider mangler redigerte innlegg. Derfor leses
alle relevante sider til `has_next=false` eller en synlig grense. Gjentatte sider,
tomme sider med `has_next=true` og ufullstendige kommentartråder godtas ikke som
komplett dekning. Begrensninger returneres til agenten; gammelt arkivinnhold brukes
aldri som erstatning. Hvert nytt oppslag henter fra kilden. Kortlivede MCP-markører
fortsetter bare det pågående oppslagets resultat, uten varig meldingslagring.

Person- og deltakelsesoppslag, på- og avmelding og kildebevis står i
[funksjonsmatrisen](service-capabilities.md#hoopit). Skrivetestene er syntetiske.
