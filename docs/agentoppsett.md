# La kodeagenten gjøre installasjonen

Dette passer for en lokalt kjørende kodeagent, for eksempel Codex eller
Claude Code, som har tilgang til filer og terminalen på datamaskinen din. En vanlig
nettchat kan ikke gjøre installasjonen alene.

Last ned kildekoden, åpne prosjektmappen med kodeagenten, og gi den denne
beskjeden:

> Installer familiekilder-mcp fra https://github.com/familiekilder/familiekilder-mcp i `~/familiekilder-mcp`. Prosjektet krever Python 3.11+ og Brave, Google Chrome eller Microsoft Edge, på macOS, Windows eller Linux. Kjør `scripts/setup.py` med Python i bakgrunnen. Veiviseren åpnes automatisk. Innlogging og resten av oppsettet gjør jeg selv der.

På Windows må nettleseren være installert for alle brukere, og på Linux fra
leverandørens egen pakke. Windows, Linux og Chrome og Edge på Mac er nytt og
ikke prøvd med ekte innlogging ennå; se
[oppsettsveiledningen](kom-i-gang.md#nettleseren).

Veiviseren legger til MCP-koblingen etter ditt valg. Kodeagenten trenger ikke
redigere klientfilene manuelt. Når den lokale MCP-testen er grønn, starter du
AI-klienten på nytt og velger familiekilder-mcp i en lokal samtale.

Du trenger ingen API-nøkkel til en AI-leverandør for selve oppsettet. Den
AI-klienten du bruker, har sine egne vilkår og eventuelle abonnementskostnader.
