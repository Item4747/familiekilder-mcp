# Fornyelse og målbar levetid

Målet er å holde innloggingen i live så lenge leverandøren tillater, og kunne
forklare avbrudd. En gyldig access-utløpstid sier ingenting om hvor lenge
refresh-tokenet varer.

## Hva som er kartlagt

| Kilde | Tilgjengelig bevis | Konsekvens |
| --- | --- | --- |
| Vigilo | Lokal økt har access `expires_at`; refresh-tokenet er opakt. Den gamle øktfilen bevarer ikke separat refresh-utløp. | Maksimal/absolutt levetid og inaktivitetsfrist er ukjent. Nye svar registrerer eksplisitte tidsfelter. |
| Skolemelding | Lokal portaløkt har `access_expires_at`; portalens tokenfornyelse er forskjellig fra ID-porten-innloggingen. | Ikke bruk ID-portens levetid som portalens levetid. `invalid_grant` beviser avvisning, ikke årsaken. |
| IST Home | Lokal økt har `issued_at` og access `expires_in`; ingen bevart separat refresh-utløp. | Ingen dokumentert maksimum eller sikkert replayvindu for denne klienten. |
| Spond | Faktiske API-svar og lokal økt har separate access-/refresh-utløp. | Bruk konkret `refresh_expires_at`. Ett slikt tidspunkt beviser ikke en absolutt grense for hele økten eller en rullerende garanti. |
| Hoopit | Firebase ID-token + separat refresh-token. Firebase dokumenterer langvarige økter uten en generell fast månedsfrist; kontosletting, deaktivering, vesentlige kontoendringer og tilbakekalling kan avslutte dem. | Ingen oppdiktet refresh-utløpsdato eller keepalive-trafikk. |

Kilder (kontrollert september 2026):
[Firebase](https://firebase.google.com/docs/auth/admin/manage-sessions),
[OAuth 2.0](https://www.rfc-editor.org/rfc/rfc6749.html#section-5.1),
[OAuth sikkerhetspraksis](https://www.rfc-editor.org/rfc/rfc9700.html#section-4.14.2)
og [ISTs innloggingsveiledning](https://www.ist.com/no/ist-home).
Offentlige søk fant ingen klientspesifikk maksimal levetid, inaktivitetsfrist
eller replaygaranti fra Vigilo, Skolemelding eller IST. Ikke fyll inn med tredjepartstall; en slik
regel krever et faktisk tokenfelt eller leverandørdokumentasjon.

Den opprinnelige innloggingen beviser at vi mottok refresh-token. Den beviser ikke
at vi mottok en refresh-utløpstid. Eldre svar ble filtrert og er ikke en komplett
historikk; vi kan derfor ikke i ettertid avgjøre om et utelatt felt var til stede.

## Sikker, vedvarende fornyelse

### Vigilo: appklient i desktop-nettleser

`auth vigilo browser-login` bruker den eksisterende appklientens
autorisasjonskodeflyt fra en desktop-nettleser.
Klientregistreringen, `openid vigiloprofile offline_access`, returadressen og
backendens tokenutveksling/refresh beholdes. Dette bytter innloggingsflate;
det bytter ikke til den ordinære webportalens klient og stille nettleserfornyelse.
Samme registrering og scopes er grunnlag for å beholde appflyten, men er ikke
et målt bevis på identisk maksimal refresh-levetid.

Rapportér bare mottatte tidsfelter og målt kontinuitet; et opakt refresh-token uten
utløpsfelt gir ikke en kjent maksimumsfrist.

### Vigilo: målt nettleser mot app

`auth vigilo browser-login` virker uten Android: BankID, tokenutveksling,
userinfo, tenant og fornyelse med rotasjon i en ny prosess etter at nettleseren
er lukket. Vigilo svarer på `app://ch-parent-android.vigilo.no/` med avsluttende
skråstrek, og `_matches_callback` godtar den; med eksakt sti så brukeren en hvit
side.

Levetidsmåling:

- Appklienten gir de samme svarene i desktop-nettleseren som fra en
  Android-enhet: samme klient og scopes, `expires_in: 600`, opakt refresh-token
  på 32 tegn og ingen refresh-frist. Android er ikke et krav i protokollen.
- Begge innloggingene holdt i minst 18,7 døgn med fornyelser uten feil. Overlevering
  fra den offisielle appen ble bare prøvd i noen minutter, så levetiden er ikke
  målt. Den ordinære webportalen (`.WEB`-klienten) gir ikke refresh-token.
- Innloggingen varer ikke evig. 7. november 2026 avviste Vigilo et refresh-token
  med `invalid_grant` etter 18,8 døgn uten bruk; innloggingen var 37,9 døgn
  gammel. Årsaken er ukjent: en inaktivitetsgrense på 4,1–18,8 døgn, en absolutt
  grense på 19,1–37,9 døgn, eller tilbakekalling.
- Målinger av varighet trenger en egen stabil datarot (`--root`); øktfiler kan
  ikke gjenskapes når en midlertidig mappe er slettet.

Lærdommer fra to lokale feil som så ut som utlogging:

- Tidskontroller trenger toleranse. Macens klokke lå noen millisekunder bak
  utsteder, og `nbf`-kontroll uten slingringsmonn ga `refresh_failed` som ikke
  løste seg selv. Vigilo, IST og Hoopit tillater nå
  `NOT_BEFORE_LEEWAY_SECONDS` (60 s), både for `nbf` og for `iat` i PyJWT.
- Eierbinding kan ikke bygge på vertsnavn på Mac, fordi `socket.gethostname()`
  følger nettverket. På macOS bindes eierfilen til `IOPlatformUUID`; eldre
  eierfiler oppgraderes første gang `LocalHostName` stemmer, og en oppgradert
  fil godtas bare med samme maskin-ID. Andre plattformer bruker vertsnavn.
  Eldre kode avviser en oppgradert eierfil.

### Lagring og eierskap

- Hver kilde har én prosesslås rundt fornyelsen og en eierfil bundet til maskin og
  kanonisk prosjektrot. En kopiert installasjon får ikke fornye samme tokenrekke.
  Bruk én autoritativ installasjon; ikke gjenopprett gamle tokenfiler fra backup.
- Vigilo, IST, Spond og Hoopit lagrer mottatt tokensvar som privat credential
  (journal) før videre validering, bundet til forrige økt og refresh-transaksjon.
  Skolemelding gjør det samme i refresh-markøren. Bindingen bruker credential- og
  identitetsfelter, så ny appversjon eller nye vedleggsverter bryter den ikke.
  Gamle journaler oppgraderes bare når bindingen kan verifiseres; en brutt eldre
  binding repareres ikke automatisk.
- Ved omstart valideres det lagrede svaret på nytt uten ny POST med det gamle
  tokenet. Signatur og alle lagrede bruker- og organisasjonsbindinger (for Vigilo
  `sub`, `personId` og `tenantId`) kontrolleres før aktivering. Er access-tokenet
  utløpt, kontrolleres signaturen mot mottakstiden og identiteten mot forrige økt,
  og så fornyes det med det nye refresh-tokenet.
  Et utløpt access-token brukes aldri til innhenting.
- Tilkoblingsavslag, DNS- eller TLS-feil før sending tillater nytt forsøk.
  Timeout etter mulig sending, ugyldig svar og ukjent utfall gir ingen blind
  replay. HTTP 5xx beviser ikke at tokenet er ubrukt.
- `invalid_grant` skilles fra usikkert utfall. Usikre Vigilo- og IST-fornyelser
  vises som teknisk utilgjengelig, ikke som utløpt innlogging.
- Private svar slettes etter fullført lagring eller ny innlogging, import eller
  utlogging. De havner aldri i logg, stdout eller chat. Alt ligger under
  `.private` med katalogmodus 0700 og filmodus 0600.
- Et helt tapt svar kan ikke gjenskapes. Gamle sperrer uten lagret svar åpnes
  ikke uten bevis.

## Trafikk og vedlikehold

MCP-oppslag fornyer access-token ved behov, under kildens prosesslås. Før et
nytt forsøk gjenopprettes et eventuelt lagret tokensvar. Gamle uavklarte sperrer
uten lagret svar gir ingen ny tokenforespørsel. Det finnes ingen tidsstyrt
innhenting eller keepalive.

`auth KILDE refresh --if-needed` er et brukerstartet engangsoppslag som også
kontrollerer access-utløp med fem minutters margin. `auth KILDE maintain` er en
manuell diagnose-/vedlikeholdskommando: den gjenoppretter lagrede svar eller
fornyer nær en kjent refresh-frist, med én times pause mellom proaktive forsøk.
Ingen av kommandoene registrerer tidsstyring. Vanlig `refresh` er et eksplisitt
engangsforsøk; ukjent inaktivitetsfrist utløser ingen oppdiktet keepalive.

## Historikk og vurdering av utnyttelse

`auth KILDE health` er et lesende, lokalt JSON-grensesnitt. Det viser:

- Mottatte, tillatte tidsfelter, med separate access- og refresh-felter.
- Tokenets numeriske JWT-tidsfelter tydelig merket `unverified`; de er diagnostikk,
  ikke signaturbevis eller automatisk refresh-policy.
- Observerte innlogginger/importer, fornyelsesforsøk, mottatte svar, rotasjon,
  gjenoppretting og vellykket lagring. Ingen rå HTTP-feiltekster logges.
- Kjent refresh-frist og gjenstående tid, ellers null og `not_recorded`.
- Målt varighet siden loggført innlogging/import, eller tydelig merket lokal
  startmåling for eksisterende økt. Historikken fra før innføringen er ufullstendig.

Historikken ligger i `.private/credentials/KILDE-auth-history.json`. Den inneholder
bare en tillatt liste av metadata og enveisfingeravtrykk for sammenkobling av
samme token mellom svar og lagring. Fingeravtrykk vises ikke i `health`.

De siste 256 hendelsene beholdes, sammen med summer og tidligere utløpsbevis.
Diagnostikkfeil hindrer ikke innlogging eller tokenfornyelse. Credential-journalen
er separat og må fortsatt kunne lagres og valideres. Ved erstatning av en gammel
feilformatert historikk eller en fil over lesegrensen kan statistikk gå tapt;
øktfilen og credential-journalen beholdes.

Vi kan måle kontinuitet og avbrudd mot en kjent tokenfrist. Vi kan ikke rapportere
«100 % maksimal øktlevetid» uten å kjenne den absolutte/evt. rullerende policyen og
observere den over tid. `maximum_lifetime_verified` er derfor foreløpig false.
Historikken skal ikke tilbakedateres som om gamle hendelser var målt.


## Skolemelding: enhetsregistrering etter innlogging

Etter BankID må innloggingen registrere enheten, slik appen gjør:
`POST /api/deviceinfo/save` med `OsVersion` og `AppVersion` gjennom den
autentiserte API-klienten, også når push ikke er tillatt. Uten dette avviser
Skolemelding refresh med `invalid_grant` (HTTP 400), og beskrivelsen omtaler
«device». Appen registrerer enheten på samme måte etter innlogging.

Innloggingen registrerer enheten med det nye access-tokenet før økten aktiveres
lokalt. `OsVersion` beskriver faktisk vert, ikke en oppdiktet Android-enhet. Tomt
2xx-svar godtas. Feil i registreringen aktiverer ikke delvis innlogging og
bevarer tidligere økt og refresh-sperre. Ingen push, biometri eller
kildehandling slås på.

Refresh-kallet sender `grant_type`, `refresh_token`, `client_id`, `app_version`
og `username`, med `Content-Type: application/x-www-form-urlencoded` og
`accept: application/json`. `username` er tredje argument fra tokenutvekslingen.

Verifisert: innlogging med registrering, to eksplisitte rotasjoner med lesing i
nye prosesser, og senere automatisk fornyelse i `AuthorizedClient` ved vanlig
lesing nær access-utløp.

Lærdom:

- En ny BankID-innlogging og et fungerende access-token beviser ikke varig
  fornyelse. Kontroller rotasjon og lesing i en ny prosess.
- Et avvist refresh-token prøves ikke i varianter. Endre ingen felt eller
  endepunkter på mistanke; skaff nye bevis fra en ny økt.

## Automatisk fornyelse ved vanlig lesing

Vigilo, Skolemelding, Hoopit og IST Home fornyer automatisk ved vanlig
MCP-lesing etter access-utløp, uten ny innlogging. I samme kontroll (oktober 2026)
leste Spond med et fortsatt gyldig access-token, så Sponds fornyelse ved vanlig
lesing ble ikke vist der. Ingen av kontrollene viser maksimal levetid for
refresh-tokenene.

`family_connections` leser kun lokal status. Verktøybeskrivelsen sier
at `access_expired` alene ikke betyr tapt tilgang, og at faktisk
lesetilgang kontrolleres med et autorisert leseoppslag før en slik konklusjon.
