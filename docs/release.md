# Veien til 1.0

`1.0.0rc1` er en lanseringskandidat. Den er ikke en ferdig stabil utgivelse.

## Hva 1.0 skal love

Lokal lesing og søk på Mac, med egen innlogging og en AI-klient som støtter
MCP over stdio. Python 3.11 eller nyere kreves. PDF-lesing krever Poppler, og
Word-lesing krever LibreOffice. Førstegangsoppsettet krever teknisk erfaring.

Kommandolinjen, navnene på leseverktøyene og de dokumenterte feltene i svarene
er det offentlige grensesnittet. Brytende endringer varsles med en ny
hovedversjon. Python-modulene er foreløpig interne. Endringer hos
kildetjenestene kan kreve feilrettinger selv om vårt grensesnitt står fast.

FOSS 1.0 skal bare tilby lesing og søk. Sending av meldinger, fravær og
deltakelsessvar inngår ikke. Eksisterende skrivekode beholdes som intern
bibliotekskode, men kan ikke aktiveres gjennom den selvstendige kommandolinjen eller MCP-serveren.

Oversettelse og forklaring gjøres av brukerens AI-assistent. Denne pakken gir
assistenten tilgang til kildene; den inneholder ingen egen språkmodell.

## Før stabil utgivelse

- Den selvstendige CLI-en og MCP-serveren avviser skriving, også fra eldre
  profiler. Den lokale oppsettsveiviseren må kontrolleres med en ny profil.
- En ny bruker må kunne skaffe og importere klientkonfigurasjon uten tilgang
  til utviklerens maskin eller private oppsett. Se [klientoppsett](klientoppsett.md).
- Innlogging, liste, meldingsdetalj og ett vedlegg må bekreftes per tjeneste som
  skal omtales som klar. Bruk egen konto, eller en uttrykkelig avtalt testkonto,
  og noter versjon, dato og resultat uten personopplysninger. Testene i repoet
  gir ikke denne bekreftelsen. Ingen ekte skrivehandling trengs for leseløftet.
- Den tekniske sjekken under må bestå på Python 3.11 og 3.14 på Mac.
- Velg utgiverkonto og kanal. Ved publisering under separat identitet må
  også eiernavn, historikk og offentlige lenker være vurdert. Et tomt
  forfatterfelt i pakken endrer ikke eieren av GitHub-repoet.

Felles publiseringsdato eller stort funksjonsomfang er ikke et krav. Tjenester
som mangler fungerende oppsett eller bekreftelse, må merkes som utprøvende.

## Teknisk sjekk

Installer [uv](https://docs.astral.sh/uv/getting-started/installation/) og kjør
fra prosjektmappen:

```sh
python3 scripts/check-release --python python3.11
python3 scripts/check-release --python python3.14
```

Sjekken bygger wheel og kildepakke fra en ren kopi, fjerner lokale fileiernavn
fra kildepakken, installerer wheelen i et nytt miljø og kjører alle testene
mot den installerte pakken. MCP-testen starter serveren med en tom profil og
utfører et verktøykall. Ingen ekte kildetjenester kontaktes. Resultater og
bygde pakker legges i `dist/`; de sendes ingen steder.

GitHub-workflowen kjører samme sjekk på Mac med begge Python-versjonene,
ved hver push til `main` og hver PR. Tagger starter ingen egen kjøring, fordi
de peker på en commit som alt er kontrollert fra `main`.
Den publiserer ikke pakker og trenger ingen publiseringsnøkkel.

## Når punktene er oppfylt

1. Sett `version` i `pyproject.toml` og `__version__` i pakken til `1.0.0`.
2. Oppdater endringsloggen og kjør den tekniske sjekken på nytt fra riktig commit.
3. Publiser fra den valgte kontoen og merk samme commit med `v1.0.0`.
4. Hvis pakken legges på PyPI, bruk den faktiske repo-adressen i pakkemetadata
   og kontroller README-lenkene der. Bruk PyPIs
   [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) for CI-publisering.

En stabil utgivelse skal ikke flyttes til en annen commit. Feil rettes i en
ny versjon, for eksempel `1.0.1`.
