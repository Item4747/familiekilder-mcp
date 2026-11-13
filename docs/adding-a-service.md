# Legge til en ny tjeneste

Denne veiledningen er for deg som vil koble en ny tjeneste (for eksempel MyKid)
til familiekilder-mcp: hvilke filer en integrasjon består av, hvilke kontrakter de
må oppfylle, og hvor tjenesten registreres. Eksemplene bruker kilde-ID `mykid`
og verktøyprefikset `mykid_` (verktøynavn lages med `source.replace("-", "_")`).

Ekte svar fra tjenesten skal ikke inn i Git, heller ikke som fixture. Lag
syntetiske svar med samme form. Øvrige regler står i [AGENTS.md](../AGENTS.md).

## Mønster å kopiere

- **Spond** (`spond.py`, `spond_auth.py`, `live_spond_reader.py`): malen når
  økten importeres som fil. Feilene arver fra `source_adapter`, HTTP går gjennom
  `api_transport.ApiTransport` med en liste over tillatte adresser. Ikke kopier
  `spond_discovery` eller varslene.
- **IST Home** (`ist_home.py`, `ist_oauth.py`, `live_ist_reader.py`): den
  korteste lesingen, med ett snapshot og én elementtype.

## 1. Filene i en integrasjon

`sources.module` laster modulene ved navn fra pakken `familiekilder`.

| Fil | Ansvar |
|---|---|
| `src/familiekilder/mykid.py` | API-klient og normalisering. En klient som henter token, fornyer én gang ved 401 og sender kall gjennom `ApiTransport`. Funksjoner som henter lister og detaljer og gjør råsvar om til normaliserte elementer. Faste grenser for sider, antall og bytes. |
| `src/familiekilder/mykid_auth.py` | Innlogging og økt (se «Auth-kontrakten»). Lagrer økten i `.private/credentials/mykid-session.json`. |
| `src/familiekilder/live_mykid_reader.py` | Leserkontrakten (se under). Tynn: kaller funksjonene i `mykid.py`. |
| `src/familiekilder/live_mykid*.py` | Valgfritt. Egne MCP-verktøy ut over de tre generiske, og skrivehandlinger. Hver modul har `register(server, runtime)`. Mønster: `live_spond_messages.py`, `live_spond_participation.py`, `live_ist.py`. |
| `tests/test_mykid.py`, `tests/test_mykid_auth.py` | Syntetiske tester (se del 4). |

### Leserkontrakten

Kontrakten (`connect`, `list_items`, `get_item`, `download` og valgfrie kroker)
står i docstringen øverst i `src/familiekilder/sources.py`. Felles hjelpere
ligger i `live_readers.py`. `live_readers.provider` legger lås per kilde
(`private_source_lock`) og eventuell pause mellom kall rundt `connect`, så
klienten skal ikke ta en egen lås. Modulene lastes ved navn først når de trengs
(`sources.module`, `sources.reader`). `live_ist_reader.py` er kortest å kopiere.

### Auth-kontrakten

Kontrakten (`session_path`, `configure_client`, `local_session_for_use`,
`refresh_session`, `auth_status`, `logout` og flere) står i docstringen øverst i
`src/familiekilder/auth_common.py`, sammen med felles hjelpere. Modulen trenger
også én måte å lage den første økten på:

| Vei | Funksjoner i auth-modulen | `auth_commands` | Brukes av |
|---|---|---|---|
| OAuth med callback | `consent_status`, `record_consent`, `begin_login`, `login_with_callback` | `login`, `browser-login`, `android-login` | Vigilo, Skolemelding, IST Home |
| Økt hentet fra nettleser | egen gren i `browser_bootstrap.bootstrap_source`, og `import_session` | `browser-bootstrap`, `import-session` | Spond, Hoopit |
| Import av øktfil | `import_session(root, path, *, replace_existing)` | `import-session` | Spond, Hoopit |

For et første bidrag holder import av øktfil. `auth_status(root)` virker uten
nettverk og uten å opprette filer, og gir minst `configured`, `authenticated`,
`session_present`, `access_expired` og `refresh_blocked` (se
`spond_auth.auth_status`). Pakk import, fornyelse og utlogging inn i
`@auth_lifecycle.operation`, slik `spond_auth` gjør, så `auth mykid health` og
`maintain` ser hendelsene.

## 2. Kontrakten for innhold

### Normalisert element

Lesefunksjonene returnerer elementer (dict) med disse feltene. Se
`ist_home.post_metadata` og `spond.normalize_post`.

| Felt | Krav |
|---|---|
| `source` | `"mykid"`. |
| `source_id` | Stabil, unik tekst per element (≤ 1000 tegn), for eksempel `post:123`. Den samme ID-en kan ikke forekomme to ganger i samme oppslag. |
| `thread_id`, `child`, `sender`, `topic` | Tekst eller `None`. |
| `published_at`, `changed_at` | ISO-tid med tidssone. Normaliser med `core.parse_iso`. Listen filtreres på `changed_at` eller `published_at`. |
| `title`, `body` | Tekst. Gjør HTML om med `core.html_to_text`. Tekstfilteret `query` søker i disse feltene. |
| `type` | `announcement`, `message`, `event`, `absence` eller `payment`. Brukes av filteret `kind`. |
| `deadline`, `event_start`, `event_end`, `location`, `action_required`, `source_url`, `date_note` | Verdi eller `None`. |
| `attachments` | Liste fra `live_readers.attachment_metadata` (se under). |
| `content_complete` | Valgfritt, standard `True`. Sett `False` når innholdet er avkortet. Da lagres elementet ikke i cachen. |
| `reference` | Settes med `live_readers.ref_item(item, ref)`. |

Kildeinnhold er ubetrodd data. `Runtime.expose` legger selv til
`source_content_is_untrusted`, `observation_token` og `source_content_hash`.

### Referanse og stabil ID

`reference` sendes tilbake til `mykid_get_item` og `mykid_read_attachment`, og
kan lagres av den som bruker serveren. Den skal bare inneholde ID-er:

- Tillatte nøkler (`live_readers.default_reference_values`): `kind`, `id`,
  `child_id`, `child_ids`, `cid`, `chat_id`, `msg_num`, `occurrence_id`.
  Trenger tjenesten andre nøkler, lager leseren `reference_values(reference)`
  (se `live_vigilo_reader.reference_values`).
- Verdiene er tekst, tall, bool eller `None`, og JSON-teksten er høyst 4096 tegn
  (`live_readers.stored_reference`).
- Kontroller hver ID med `live_readers.identifier`: URL-sikker og høyst 256
  tegn. Lag aldri en URL ut fra noe agenten har sendt.
- Ikke bruk `kind: "notification"` (se Kjente skjevheter).

### `complete` og sider

`list_items` returnerer `(items, complete)`. `complete=True` betyr at hele det
avgrensede oppslaget ble gått gjennom, ikke at all historikk er hentet. Sett
`False` når en side feilet, en grense ble nådd eller elementer manglet sikker
tidsangivelse (som `live_skolemelding_reader.list_items`). Ufullstendige lister
lagres ikke i cachen (`Runtime._load_list`: `'cacheable': complete`).

MCP-sidene (`limit`, `next_cursor`) lages av `Runtime.list_items`. Tjenestens
egne sider blar integrasjonen gjennom selv, med en fast øvre grense (se
`ist_home.MAX_PAGES` og `MAX_POSTS`).

### Vedlegg

1. Ved normalisering: `attachment_metadata(spec)` gir `attachment_id`
   (fingeravtrykk av ID/URL-sti og navn), `filename`, `mime_type` og `_spec`.
   Nøkler som starter med `_` vises aldri og lagres aldri i cachen
   (`live_readers.normalized_item`, `Runtime.expose`). Utløpende
   nedlastings-URL-er hører hjemme i `_spec`.
2. Ved lesing: `live_readers.download_live` henter elementet på nytt, kontrollerer
   `expected_content_hash` og kaller leserens `download`. Den bruker
   `fetch_attachment(item, attachment_id, directory, fetch)`, der
   `fetch(spec, target)` laster ned, holder en bytegrense (som
   `hoopit.MAX_ATTACHMENT_BYTES`) og gir `(path, mime)`.
3. Visning, PDF-vinduer og cache er felles (`live_attachments.render`,
   `pdf_windows.py`).

### Konto, cache og snapshot

- **Konto:** `read_cache.account_scope(root, source)` lager et hash av
  identitetsfeltene i `.private/credentials/<source>-session.json` og
  `<source>-client.json` eller `<source>-provider.json`. Bruk disse filnavnene,
  og lagre en stabil kontoidentitet i `subject` eller `profile_id`. Da overlever
  cache og kvitteringer tokenrotasjon, men ikke kontobytte.
- **Lesecache:** `Runtime._fetch_read` lagrer lister i 60 sekunder
  (`read_cache.LIST_FRESHNESS`) og detaljer i 300 sekunder (`DETAIL_FRESHNESS`).
  Dette er felles og krever ingen kode i integrasjonen. Se
  [mellomlagring](read-cache.md).
- **Felles snapshot:** Send hvert lesende HTTP-kall gjennom
  `sync_snapshot.memo_read(root, "mykid", key, loader)`, slik
  `skolemelding.AuthorizedClient` og `ist_home.graphql` gjør. Innlogging,
  token og skrivekall skal aldri gå gjennom `memo_read`.

### Endringer over tid

`Source.streams` er `("feed",)` som standard; `"chat"` krever `discover_chats`.
Krokene `discover_feed` og `discover_chats` brukes av en vert som vil følge med
på endringer mellom oppslag, og tar `observe=None` så verten kan lagre hver sides
elementer sammen med sidemarkøren (`chat_discovery.py`, `spond_discovery.py`).
Kontrakten står i docstringen i `sources.py`. MCP-serveren selv trenger dem ikke.

### Feilklasser (`source_adapter.py`)

| Klasse | Når |
|---|---|
| `SourceLoginRequired` | Økten er avvist, eller fornyelsen er avslått. Brukeren må logge inn på nytt. |
| `SourceFatalConfigurationError` | Tjenesten avviser selve klient- eller appregistreringen. Ny innlogging hjelper ikke. |
| `SourceUnavailable` | Alt annet som ikke handler om innlogging: elementet finnes ikke, ugyldig svar, pause etter for mange kall. |
| `SourceBudgetExceeded` | Et avgrenset oppslag stoppet før siste side eller siste element (se `hoopit.RequestBudgetExceeded`). |

Lag egne underklasser, som `spond_auth.SpondReauthenticationRequired`.
`source_diagnostics.failure` bruker klassen til å sette `login_required`,
`configuration` eller `collection_limit`. Meldingene vises for brukeren: norsk,
uten token, URL eller råsvar. `source_server.safe_tool` gjør andre unntak om til
en generell feil.

### Skrivehandlinger (valgfritt, ikke i første bidrag)

En handling registreres bare når kontrakten er verifisert. Den bruker
`runtime.config.require(source, category)` (kategorier i
`live_config.CATEGORIES`) og `ledger.reserve_operation`/`operation_result`, så
den ikke sendes to ganger, og leser resultatet tilbake. Mønster:
`live_spond_participation.set_participation`. Modulen føres i `Source.actions`,
og det som er verifisert, i `verified` og `implemented`. Les
[funksjonsmatrisen](service-capabilities.md). Uten et eksplisitt testmål fra
brukeren testes skrivekall bare mot falske transporter.

## 3. Registrering

### A. Egne moduler og én `Source`

Legg til én oppføring i `REGISTRY` i `src/familiekilder/sources.py` (feltene
er forklart i `Source`). For et første, lesende bidrag:

```python
Source(
    "mykid", "MyKid", "mykid_auth", "live_mykid_reader",
    letter="M", description="Beskjeder fra barnehagen", kind="school",
    auth_commands=frozenset({"configure", "import-session", "refresh", "status", "logout"}),
    provider_bundle=False,
),
```

`provider_bundle=False` holder tjenesten utenfor `core.PROVIDER_SOURCES`, så
signerte providerpakker ikke krever den. Fra registeret lages `core.SOURCES`,
`source_adapter.AUTH_MODULES`, `auth_lifecycle.SOURCES`, `auth_cli`,
`live_traffic.SOURCES`, `live_actions.capabilities` og
`session_estimates.estimate`. `sources.web_script()` lager et `/services.js` med
navn (`label`), bokstav (`letter`), beskrivelse (`description`) og gruppe
(`kind`: `school`, `sport`, `mail` eller `other`) for nettsider som viser
tjenestene; `tests/test_web_script.py` krever at alle er satt. Verktøyene
`mykid_list_items`, `mykid_get_item` og `mykid_read_attachment` lages av
`source_server.register_source_reads`. Aktiver med `familiekilder-mcp configure mykid`.

### B. Andre steder, når tjenesten trenger dem

- [ ] `browser_bootstrap.bootstrap_source`: bare med `browser-bootstrap`.
      Passordutfylling krever i tillegg `password_origins` i `Source`.
- [ ] `chat_discovery.py`: bare hvis `streams` har `"chat"` og leseren vil
      bruke den felles chatgjennomgangen i stedet for en egen `discover_chats`.
- [ ] `provider_bundle.py` og `provider_contract.py`: bare med
      `provider_bundle=True`. Krever en ny signert pakke.

## 4. Testing uten ekte konto

Bruk midlertidige kataloger, falske transporter og oppdiktede data. Falske
transporter som flere testfiler bruker, ligger i
`tests/support/familiekilder_testing/`; importer dem derfra, aldri fra en annen
testfil.

| Hva | Fil å kopiere | Mønster |
|---|---|---|
| API-klient og normalisering | `tests/test_spond.py` (`familiekilder_testing.spond`: `FakeCollectorTransport`, `FakeClock`) | Falsk transport svarer per URL, og testen kontrollerer kall og elementer. |
| Innlogging og fornyelse | `tests/test_spond_auth.py`, `tests/test_ist_oauth.py` (`familiekilder_testing.spond_auth`, `.ist_oauth`) | Øktfiler i `tempfile.TemporaryDirectory()`, rotasjon, avvisning og `auth_status` uten nettverk. |
| Snapshot-gjenbruk | `tests/test_sync_snapshot_adapters.py` | Lesekall gjenbrukes, men token- og skrivekall gjør det ikke. |

Legg `mykid` til i den eksakte kildetuppelen i `tests/test_source_adapter.py`.

Den som har en ekte MyKid-konto, kan lese med egne credentials lokalt for å lære
formen på svarene. Rådata, navn og ID-er fra kontoen skal ikke inn i repoet.

## 5. Kjøre testene

```sh
scripts/bootstrap-local-venv         # én gang: .private/venv
scripts/test tests/test_mykid.py     # bare de nye testene
scripts/test                         # alle testene
uvx ruff@0.17.4 check .
```

## 6. Prøve tjenesten med egen konto

```sh
familiekilder-mcp configure mykid
familiekilder-mcp auth mykid import-session --session-file /privat/mappe/mykid-økt.json
familiekilder-mcp connections
familiekilder-mcp mcp serve
```

Øktfilen må ligge i en privat mappe (0700) med filmodus 0600. Uten konto kan du
bare teste med falske transporter.

## Kjente skjevheter

Registrering som er inkonsekvent eller lett å overse.

- **`provider_bundle` er `True` som standard.** En ny kilde havner da i
  `core.PROVIDER_SOURCES`, og alle eksisterende pakker blir ugyldige.
  `provider_contract` slår opp `_PACKAGE_IDS[source]`. Sett
  `provider_bundle=False` til tjenesten har en signert providerprofil.
- **`auth_commands` kan ikke være tom.** `auth_cli._run_generic_auth` slår opp
  `_AUTH_CAPABILITIES[source]`. Bare Gmail slipper, fordi `source_cli.py` sender den
  videre før oppslaget.
- **Kildelister skrevet for hånd:** `browser_bootstrap.bootstrap_source`.
- **IST Home bruker ikke feilklassene fra `source_adapter`.**
  `ist_home.IstHomeAuthError` og `ist_oauth_protocol.OAuthError` arver fra
  `FamiliekilderError`. Avvist innlogging blir derfor `source_error` i
  `source_diagnostics.failure`, ikke `login_required`.
- **`kind: "notification"` er reservert.** Bare kilder med `notifications=True`
  leser slike referanser fra listen (`live_readers._require_detail_reference`,
  `Runtime.read_notification`).
- **`chat_discovery._discover` kjenner bare Spond og Hoopit.** Den velger
  sidefelt med `source == 'spond'` og bruker Hoopit-formen for alt annet.
- **To HTML-til-tekst-funksjoner.** `core.html_to_text` brukes av Spond,
  Skolemelding og nye kilder. Vigilo og IST Home bruker
  `core.html_to_text_unescaped`, som avkoder entiteter én gang til og gir `""`
  for tom tekst. Lagrede innholdshasher bygger på dette; slå dem ikke sammen.
- **Hoopit har et eget navn på fornyelsesmerket.** `auth_lifecycle.report`
  leser `hoopit-refresh-state.json`, ellers `<source>-refresh-blocked.json`.
