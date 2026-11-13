# La kodeagenten gjøre installasjonen

Dette passer for en lokalt kjørende kodeagent, for eksempel Codex eller
Claude Code, som har tilgang til filer og terminalen på Macen din. En vanlig
nettchat kan ikke gjøre installasjonen alene.

Last ned kildekoden, åpne prosjektmappen med kodeagenten, og gi den denne
beskjeden:

> Installer familiekilder-mcp fra denne prosjektmappen og start det lokale
> oppsettet. Les README og docs/kom-i-gang.md først. Kontroller at Python 3.11
> eller nyere og Brave finnes. Be meg om å installere det som eventuelt
> mangler, eller hjelp meg med det når jeg har godkjent det. Kjør scripts/setup.
> La meg velge tjenester, oppsettsfiler og AI-klient i veiviseren. Jeg gjør
> innlogging og BankID selv. Ikke kopier eller vis passord, tokens eller
> oppsettsfiler i samtalen. Ikke gi tilgang til skriving eller bruk beskjeder
> fra ekte kontoer som tester.

Veiviseren legger til MCP-koblingen etter ditt valg. Kodeagenten trenger ikke
redigere klientfilene manuelt. Når den lokale MCP-testen er grønn, starter du
AI-klienten på nytt og velger familiekilder-mcp i en lokal samtale.

Du trenger ingen API-nøkkel til en AI-leverandør for selve oppsettet. Den
AI-klienten du bruker, har sine egne vilkår og eventuelle abonnementskostnader.
