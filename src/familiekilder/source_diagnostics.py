"""Fixed, private-safe explanations for incomplete read coverage.

Classification uses exception types and structured codes, never exception text.
An unknown adapter failure is not evidence that authentication has expired.
"""
from datetime import UTC, datetime, timedelta

from httpx import NetworkError, TimeoutException

from .api_transport import ApiHTTPError, ApiTransportError, ApiRequestNotSent
from .private_lock import PrivateLockBusy
from .source_adapter import SourceBudgetExceeded, SourceFatalConfigurationError, SourceLoginRequired

CODES = {'login_required', 'configuration', 'rate_limit', 'daily_quota',
         'backend_error', 'transient', 'work_budget', 'pagination', 'metadata_budget',
         'message_missing', 'incomplete_read', 'source_error', 'collection_limit',
         'setup_verification', 'access_denied', 'traffic_budget'}
BACKLOG = {'work_budget', 'pagination', 'metadata_budget'}
RECOVERABLE = BACKLOG | {'transient', 'backend_error', 'rate_limit', 'traffic_budget'}
MESSAGES = {
    'traffic_budget': ('Appen har tatt en pause for å begrense trafikken til tjenesten.',
                       'Fremdriften er lagret. Innhentingen kan fortsette etter oppgitt pausetid.'),
    'login_required': ('Tjenesten krever ny innlogging for å gi lesetilgang.', 'Koble til tjenesten på nytt for å gi lesetilgang.'),
    'configuration': ('Tjenesten avviste appens tilkoblingsoppsett.', 'Tilkoblingsoppsettet må rettes før innhentingen kan fortsette.'),
    'setup_verification': ('Appens lesetilgang er ikke ferdig verifisert.', 'Appen prøver å verifisere lesetilgangen automatisk ved neste synkronisering.'),
    'access_denied': ('Tjenesten avviste appens lesetilgang.', 'Appen trenger en teknisk retting for å hente innholdet. Du trenger ikke logge inn på nytt nå.'),
    'rate_limit': ('Tjenesten har bedt oss ta en pause i innhentingen.', 'Fremdriften er lagret. Neste planlagte synkronisering prøver igjen etter pausen.'),
    'daily_quota': ('Tjenestens dagskvote for lesing er nådd.', 'Fremdriften er lagret. Innhentingen fortsetter ved en senere planlagt synkronisering.'),
    'backend_error': ('Tjenesten svarte med en midlertidig serverfeil.', 'Fremdriften er lagret. Neste planlagte synkronisering prøver igjen.'),
    'transient': ('Innhentingen ble avbrutt av et midlertidig tilgangs- eller nettverksproblem.', 'Fremdriften er lagret. Neste planlagte synkronisering prøver igjen.'),
    'work_budget': ('Beskjeder er hentet, men det er mer innhold igjen.', 'Innhentingen fortsetter fra lagret fremdrift ved neste planlagte synkronisering.'),
    'pagination': ('Beskjeder er hentet, men flere sider gjenstår.', 'Innhentingen fortsetter fra lagret fremdrift ved neste planlagte synkronisering.'),
    'metadata_budget': ('Beskjeder er hentet, men flere meldingsoversikter gjenstår.', 'Innhentingen fortsetter fra lagret fremdrift ved neste planlagte synkronisering.'),
    'message_missing': ('Enkelte meldinger finnes ikke lenger i tjenesten.', 'Resten behandles. Manglende meldinger tolkes ikke som avlysninger.'),
    'incomplete_read': ('Noe innhold kunne ikke leses fullstendig.', 'Resten behandles. Appen beholder uferdig arbeid og prøver igjen ved neste synkronisering.'),
    'source_error': ('Tjenestens innhold kunne ikke leses i denne synkroniseringen.', 'Appen prøver å hente innholdet igjen ved neste synkronisering. Du trenger ikke logge inn på nytt nå.'),
    'collection_limit': ('Innhentingen nådde grensen før alt innhold var lest.', 'Dette trenger en teknisk retting i appen. Du trenger ikke logge inn på nytt.'),
}


def retry_time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(UTC).isoformat() if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def normalize(values, *, default='incomplete_read'):
    code = values.get('incomplete_reason') or default
    return {'incomplete_reason': code if isinstance(code, str) and code in CODES else 'source_error',
            'retry_at': retry_time(values.get('retry_at'))}


def failure(exc, *, at):
    code = 'source_error'
    retry_at = getattr(exc, 'retry_at', None)
    reason = getattr(exc, 'reason', None)
    if isinstance(exc, SourceFatalConfigurationError):
        code = 'configuration'
    elif isinstance(exc, SourceLoginRequired):
        code = 'login_required'
    elif isinstance(exc, SourceBudgetExceeded):
        code = 'traffic_budget' if reason == 'traffic_budget' else 'collection_limit'
    elif isinstance(reason, str) and reason in CODES:
        code = reason
    elif reason == 'http-429':
        code = 'rate_limit'
    elif reason == 'http-5xx':
        code = 'backend_error'
    elif reason == 'network':
        code = 'transient'
    elif isinstance(exc, ApiHTTPError):
        code = ('rate_limit' if exc.status_code == 429 else 'backend_error'
                if 500 <= exc.status_code <= 599 else 'source_error')
        # A raw HTTP 401/403 alone cannot establish that token refresh failed.
        if exc.retry_after_seconds is not None:
            retry_at = (at + timedelta(seconds=exc.retry_after_seconds)).isoformat()
    elif isinstance(exc, (TimeoutError, ConnectionError, NetworkError, TimeoutException,
                          ApiRequestNotSent, PrivateLockBusy)):
        code = 'transient'
    elif isinstance(exc, ApiTransportError):
        # Other transport errors include contract/allowlist rejection.
        code = 'source_error'
    cooldown = getattr(exc, 'cooldown_until', None)
    if type(cooldown) in (int, float) and cooldown > 0:
        try:
            retry_at = datetime.fromtimestamp(cooldown, UTC).isoformat()
        except (ValueError, OverflowError, OSError):
            pass
    return normalize({'incomplete_reason': code, 'retry_at': retry_at})


def presentation(rows):
    incomplete = [r for r in rows if r['status'] != 'complete']
    if not incomplete:
        return {}
    priority = ['configuration', 'access_denied', 'login_required', 'setup_verification', 'daily_quota', 'traffic_budget', 'rate_limit', 'backend_error',
                'transient', 'source_error', 'incomplete_read', 'message_missing',
                'collection_limit', 'work_budget', 'metadata_budget', 'pagination']
    diagnostics = [normalize(row, default='source_error' if row['status'] == 'unavailable'
                             else 'incomplete_read') for row in incomplete]
    chosen = min(diagnostics, key=lambda row: priority.index(row['incomplete_reason']))
    code = chosen['incomplete_reason']
    return {'issue_code': code, 'error': MESSAGES[code][0], 'next_step': MESSAGES[code][1],
            'action': 'reconnect' if code == 'login_required' else None,
            'retry_at': max((r['retry_at'] for r in diagnostics if r['retry_at']), default=None)}
