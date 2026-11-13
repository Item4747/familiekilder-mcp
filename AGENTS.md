## Tilgang

- Utvikling, testing og ferdigstilling av en endring gir ikke lov til å skrive til
  ekte kildetjenester eller slå på skriverettigheter for noen. Det krever en
  konkret bestilling fra den som eier kontoen.
- Tester bruker bare oppdiktede familier, kontoer og svar. Ekte svar fra en
  tjeneste, navn og ID-er skal ikke inn i Git, heller ikke som fixture.
- Kopier aldri innloggingsopplysninger, tokens eller nøkler, og legg dem aldri i
  kommandolinjen, logger eller Git.

## Les først

- Ny kildetjeneste: `docs/adding-a-service.md`. Kildelister lages fra
  `sources.REGISTRY`, aldri for hånd.
- Hva hver tjeneste kan, og hvordan skrivehandlinger er vernet:
  `docs/service-capabilities.md`.

## Kode

- Tekst og felter som inngår i innholdshash eller fingeravtrykk for lagrede data,
  skal gi nøyaktig samme utdata som før. En felles hjelper som erstatter lokale
  kopier, trenger en test mot den gamle koden (som `tests/test_html_to_text.py`).
- `uvx ruff@0.17.4 check .` skal være grønn. Kjør `uvx ruff@0.17.4 format` bare på
  nye filer og filer du skriver om (resten er uformatert), og aldri i samme commit
  som en funksjonsendring.
- Ikke skriv komprimerte enlinjere eller minifisert kode.
- Falske transporter som flere testfiler bruker, ligger i
  `tests/support/familiekilder_testing/`. Importer dem derfra, aldri fra en annen
  testfil.
- Kjør `scripts/test` for det du har endret, og alle testene før en endring
  merges.
- Hemmelighetsskannere oppfatter `navn = pwd.getpwnam('bruker')` og filnavn med
  «password» fulgt av en hash som hardkodede passord. Bruk en navngitt konstant
  for brukernavnet og sjekksumlinjer for låser.

## Dokumentasjon

Dokumentasjonen står i `docs/` og i `README.md`, på norsk bokmål i klarspråk, og
skrives så en ny utvikler ikke må kjenne historikken. Beskriv bare funksjoner,
kommandoer og tester i dette prosjektet; merk interne API-er som interne. Den
selvstendige CLI-en og MCP-serveren tilbyr bare lesing. Kode og kommentarer
skrives på engelsk.
