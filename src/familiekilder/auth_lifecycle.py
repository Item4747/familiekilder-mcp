"""Secret-free auth history, single-installation ownership and response journals.

The journal is a credential (0600), never a log. History has an explicit numeric
allowlist: no tokens, subjects, URLs, callback data or exception strings.
"""

from __future__ import annotations

from datetime import datetime
from contextlib import nullcontext
import base64
import functools
import hashlib
import json
import math
import re
import socket
import subprocess
import sys
import time

from .core import FamiliekilderError, project_paths
from .private_lock import private_source_lock
from .sources import REGISTRY
from .private_state import (
    atomic_write_private_json,
    credential_path,
    read_private_json,
    remove_private_file,
    PrivateStateError,
)

SOURCES = tuple(name for name, source in REGISTRY.items() if source.login != 'google')
# A token can reach us milliseconds before the second it is stamped with. One such
# rejection would otherwise block the grant for good, so tolerate ordinary clock skew.
NOT_BEFORE_LEEWAY_SECONDS = 60
TIME_FIELDS = (
    'issued_at',
    'updated_at',
    'expires_at',
    'expires_in',
    'access_expires_at',
    'refresh_expires_at',
    'refresh_expires_in',
    'refresh_token_expires_in',
    'refresh_token_expires_at',
    'refreshTokenExpiration',
    'tokenExpiration',
)
EVENTS = {
    'baseline',
    'refresh_started',
    'refresh_succeeded',
    'refresh_failed',
    'login_succeeded',
    'login_failed',
    'response_received',
    'response_saved',
    'response_recovered',
    'request_not_sent',
    'request_uncertain',
    'request_rejected',
    'import_succeeded',
    'import_failed',
    'logout_succeeded',
    'logout_failed',
}


def _path(root, source, suffix):
    if source not in SOURCES:
        raise FamiliekilderError('Ukjent auth-kilde')
    return credential_path(root, f'{source}-{suffix}.json')


def _read(path):
    if not path.exists() and not path.is_symlink():
        return {}
    return dict(read_private_json(path, max_bytes=16 * 1024 * 1024))


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()


def _mac_query(*command):
    try:
        return subprocess.run(
            command, capture_output=True, text=True, timeout=15, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ''


def _machine_identity():
    """macOS only: its hostname follows the network, unlike this hardware ID."""
    if sys.platform != 'darwin':
        return None
    found = re.search(
        r'"IOPlatformUUID"\s*=\s*"([^"]+)"',
        _mac_query('/usr/sbin/ioreg', '-rd1', '-c', 'IOPlatformExpertDevice'),
    )
    return found.group(1) if found else None


def _hostnames():
    names = {socket.gethostname()}
    if sys.platform == 'darwin':
        local_name = _mac_query('/usr/sbin/scutil', '--get', 'LocalHostName').strip()
        if local_name:
            names.add(f'{local_name}.local')
    return names


def _owner_digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def require_owner(root, source):
    path = _path(root, source, 'auth-owner')
    machine = _machine_identity()
    expected = {
        'root': str(project_paths(root).root.resolve()),
        'host': _owner_digest(socket.gethostname()),
    }
    if machine:
        expected['machine'] = _owner_digest(machine)
    old = _read(path)
    if not old:
        atomic_write_private_json(path, expected)
        return
    if 'machine' in old:
        same_machine = old['machine'] == expected.get('machine')
    else:
        same_machine = old.get('host') in {_owner_digest(name) for name in _hostnames()}
    if old.get('root') != expected['root'] or not same_machine:
        raise FamiliekilderError(
            'Tokenøkten eies av en annen installasjon; ikke bruk en kopiert økt'
        )
    if machine and 'machine' not in old:
        atomic_write_private_json(path, {**old, 'machine': expected['machine']})


def _number(value):
    # Providers sometimes encode expires_in as a JSON string.
    try:
        result = float(value) if not isinstance(value, bool) else -1
    except (ValueError, TypeError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value)
            result = parsed.timestamp() if parsed.tzinfo is not None else -1
        except (ValueError, TypeError, OverflowError):
            return None
    return result if math.isfinite(result) and 0 <= result <= 10**11 else None


def time_metadata(value):
    result = {}
    for key in TIME_FIELDS:
        n = _number(value.get(key))
        if n is not None:
            result[key] = n
    # Spond nests the two explicit expiration timestamps.
    for key, label in (('accessToken', 'access'), ('refreshToken', 'refresh')):
        nested = value.get(key)
        if isinstance(nested, dict):
            n = _number(nested.get('expiration'))
            if n is not None:
                result[label + '_expires_at'] = n
    # Decoding is diagnostic only, never validation or proof of refresh expiry.
    for key in ('access_token', 'id_token', 'refresh_token'):
        token = value.get(key)
        if not isinstance(token, str) or token.count('.') != 2 or len(token) > 100000:
            continue
        try:
            raw = token.split('.')[1]
            claims = json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)))
            if not isinstance(claims, dict):
                continue
            for name in ('iat', 'exp', 'auth_time'):
                n = _number(claims.get(name))
                if n is not None:
                    result[key + '_unverified_' + name] = n
        except (ValueError, TypeError, UnicodeError):
            pass
    return result


HISTORY_LIMIT = 256
_HISTORY_TIME_FIELDS = {
    'history_started_at',
    'last_login_observed_at',
    'last_successful_refresh_at',
    'last_failed_refresh_at',
}


def _history_entry(value):
    """Rebuild the diagnostic allowlist, including when reading older history."""
    if (
        not isinstance(value, dict)
        or not isinstance(value.get('event'), str)
        or value['event'] not in EVENTS
    ):
        return None
    instant = _number(value.get('at'))
    if instant is None or instant <= 0:
        return None
    entry = {'at': instant, 'event': value['event']}
    times = value.get('times')
    if isinstance(times, dict):
        allowed = (
            set(TIME_FIELDS)
            | {'access_expires_at', 'refresh_expires_at'}
            | {
                key + '_unverified_' + claim
                for key in ('access_token', 'id_token', 'refresh_token')
                for claim in ('iat', 'exp', 'auth_time')
            }
        )
        entry['times'] = {
            key: number
            for key, raw in times.items()
            if key in allowed and (number := _number(raw)) is not None
        }
    grant = value.get('grant_fingerprint')
    if isinstance(grant, str) and len(grant) == 64 and all(c in '0123456789abcdef' for c in grant):
        entry['grant_fingerprint'] = grant
    if type(value.get('rotated')) is bool:
        entry['rotated'] = value['rotated']
    if type(value.get('http_status')) is int and 100 <= value['http_status'] <= 599:
        entry['http_status'] = value['http_status']
    return entry


def _fold_history(summary, entry):
    summary.setdefault('history_started_at', entry['at'])
    field = {
        'login_succeeded': 'last_login_observed_at',
        'import_succeeded': 'last_login_observed_at',
        'refresh_succeeded': 'last_successful_refresh_at',
        'refresh_failed': 'last_failed_refresh_at',
    }.get(entry['event'])
    if field:
        summary[field] = entry['at']
    if entry['event'] == 'refresh_succeeded':
        summary['successful_refreshes'] = summary.get('successful_refreshes', 0) + 1
    fields = entry.get('times', {})
    if entry.get('grant_fingerprint') and any(
        key in fields
        for key in (
            'refresh_expires_at',
            'refresh_token_expires_at',
            'refreshTokenExpiration',
            'refresh_expires_in',
            'refresh_token_expires_in',
        )
    ):
        summary['grant_evidence'] = entry


def _history(root, source):
    try:
        value = _read(_path(root, source, 'auth-history'))
    except (PrivateStateError, ValueError, TypeError, RecursionError):
        value = {}
    raw_summary = value.get('summary', {})
    summary = {}
    if isinstance(raw_summary, dict):
        for key in _HISTORY_TIME_FIELDS | {'successful_refreshes'}:
            number = _number(raw_summary.get(key))
            if number is not None:
                summary[key] = int(number) if key == 'successful_refreshes' else number
        evidence = _history_entry(raw_summary.get('grant_evidence'))
        if evidence:
            summary['grant_evidence'] = evidence
    raw_events = value.get('events', [])
    events = (
        [entry for raw in raw_events if (entry := _history_entry(raw)) is not None]
        if isinstance(raw_events, list)
        else []
    )
    for entry in events[:-HISTORY_LIMIT]:
        _fold_history(summary, entry)
    return {'version': 2, 'summary': summary, 'events': events[-HISTORY_LIMIT:]}


def record(root, source, event, *, value=None, at=None, rotated=None, http_status=None):
    """Best-effort bounded diagnostics; never discard a credential operation."""
    try:
        instant = time.time() if at is None else float(at)
        entry = {'at': instant, 'event': event}
        if value is not None:
            entry['times'] = time_metadata(value)
            token = value.get('refresh_token')
            if not token and isinstance(value.get('refreshToken'), dict):
                token = value['refreshToken'].get('token', value['refreshToken'].get('value'))
            if isinstance(token, str) and token:
                entry['grant_fingerprint'] = _digest(token)
        entry.update(rotated=rotated, http_status=http_status)
        entry = _history_entry(entry)
        if entry is None:
            return
        with private_source_lock(root, source, wait=True):
            history = _history(root, source)
            history['events'].append(entry)
            while len(history['events']) > HISTORY_LIMIT:
                _fold_history(history['summary'], history['events'].pop(0))
            atomic_write_private_json(_path(root, source, 'auth-history'), history)
    except Exception:
        # Owner checks, journals and credential writes remain fail-closed outside
        # this diagnostic boundary. Their errors are never suppressed here.
        return


def operation(source, kind='refresh'):
    """Serialize auth operations and record failures without secret exceptions."""

    def decorate(fn):
        @functools.wraps(fn)
        def run(root, *args, **kwargs):
            with private_source_lock(root, source) if kind != 'login' else nullcontext():
                with private_source_lock(root, source, wait=True):
                    require_owner(root, source)
                instant = kwargs.get('at', kwargs.get('wall_time'))
                try:
                    before = _read(_path(root, source, 'session'))
                except (PrivateStateError, RecursionError):
                    before = {}  # Only diagnostic input; the provider validates credentials.
                if kind == 'refresh':
                    record(root, source, 'refresh_started', value=before, at=instant)
                try:
                    result = fn(root, *args, **kwargs)
                except Exception:
                    record(root, source, kind + '_failed', at=instant)
                    raise
                if kind in ('login', 'import', 'logout'):
                    clear_response(root, source)
                try:
                    after = _read(_path(root, source, 'session'))
                except (PrivateStateError, RecursionError):
                    after = {}
                record(
                    root,
                    source,
                    kind + '_succeeded',
                    value=after,
                    at=instant,
                    rotated=(before.get('refresh_token') != after.get('refresh_token'))
                    if before
                    else None,
                )
                return result

        @functools.wraps(fn)
        def checked(root, *args, **kwargs):
            try:
                return run(root, *args, **kwargs)
            except PrivateStateError as exc:
                if kind != 'logout' or source != 'ist-home':
                    raise
                error_name = {
                    'vigilo': 'VigiloOAuthError',
                    'ist-home': 'OAuthError',
                    'skolemelding': 'SkolemeldingAuthError',
                    'spond': 'SpondAuthError',
                    'hoopit': 'HoopitAuthError',
                }[source]
                error = fn.__globals__.get(error_name, FamiliekilderError)
                raise error(str(exc)) from None

        return checked

    return decorate


_GRANT_FIELDS = (
    'access_token',
    'id_token',
    'refresh_token',
    'subject',
    'profile_id',
    'person_id',
    'tenant_id',
    'client_id',
    'client_secret',
    'scope',
    'authority',
    'redirect_uri',
    'client_fingerprint',
    'session_binding',
    'device_unique_id',
    'userName',
    'native_handoff_id',
)


def _grant_binding(source, session):
    """Bind the exact credential pair and identity, excluding mutable metadata."""
    return _digest(
        {'source': source, 'grant': {key: session[key] for key in _GRANT_FIELDS if key in session}}
    )


def save_response(root, source, session, transaction_id, response, *, at):
    # Credential journal: failures remain fatal, independently of diagnostics.
    atomic_write_private_json(
        _path(root, source, 'refresh-response'),
        {
            'binding_version': 2,
            'session_binding': _grant_binding(source, session),
            'transaction_id': transaction_id,
            'received_at': at,
            'response': response,
        },
    )
    record(root, source, 'response_saved', value=response, at=at)


def saved_response(root, source, session, transaction_id):
    value = _read(_path(root, source, 'refresh-response'))
    if not value or not transaction_id or value.get('transaction_id') != transaction_id:
        return None
    version = value.get('binding_version', 1)
    expected = _grant_binding(source, session) if version == 2 else _digest(session)
    if version not in (1, 2) or value.get('session_binding') != expected:
        reply = value.get('response', {})
        if not isinstance(reply, dict):
            raise FamiliekilderError('Lagret refresh-svar er ugyldig')
        access = reply.get('access_token', reply.get('id_token'))
        refresh = reply.get('refresh_token')
        if (
            access
            and refresh
            and access == session.get('access_token', session.get('id_token'))
            and refresh == session.get('refresh_token')
        ):
            clear_response(root, source)
            return None
        # A committed replacement may have survived a crash before journal cleanup.
        if transaction_id in (
            session.get('refresh_transaction_id'),
            session.get('session_transaction_id'),
        ):
            clear_response(root, source)
            return None
        raise FamiliekilderError('Lagret refresh-svar tilhører ikke gjeldende økt')
    if not isinstance(value.get('response'), dict) or not _number(value.get('received_at')):
        raise FamiliekilderError('Lagret refresh-svar er ugyldig')
    if version == 1:
        # Upgrade only after verifying the original full-session binding. An old
        # hash alone cannot establish identity after its metadata has changed.
        value = dict(value, binding_version=2, session_binding=_grant_binding(source, session))
        atomic_write_private_json(_path(root, source, 'refresh-response'), value)
    return value


def clear_response(root, source):
    remove_private_file(_path(root, source, 'refresh-response'))


def observe_transport(root, source, client, *, at=None, capture_response=True):
    class Observed:
        def __getattr__(self, name):
            method = getattr(client, name)
            if name not in ('post_form', 'post_json'):
                return method

            def call(*args, **kwargs):
                try:
                    result = method(*args, **kwargs)
                except Exception as exc:
                    sent = getattr(exc, 'request_sent', None)
                    status = getattr(exc, 'status_code', getattr(exc, 'http_status', None))
                    record(
                        root,
                        source,
                        'request_not_sent'
                        if sent is False
                        else 'request_rejected'
                        if status is not None
                        else 'request_uncertain',
                        at=at,
                        http_status=status,
                    )
                    raise
                if capture_response and isinstance(result, dict):
                    try:
                        if time_metadata(result) or any(
                            k in result
                            for k in ('refresh_token', 'refreshToken', 'id_token', 'access_token')
                        ):
                            record(root, source, 'response_received', value=result, at=at)
                    except Exception:
                        # Even malformed diagnostic JWT claims must not discard
                        # a rotated reply before its credential journal is saved.
                        pass
                return result

            return call

    return Observed()


def report(root, source, *, at=None):
    now = time.time() if at is None else at
    retained = _history(root, source)
    history = retained['events']
    summary = dict(retained['summary'])
    for entry in history:
        _fold_history(summary, entry)
    session = _read(_path(root, source, 'session'))
    current = time_metadata(session)
    grant = _digest(session.get('refresh_token'))
    evidence = retained['summary'].get('grant_evidence')
    matching = [
        e for e in ([evidence] if evidence else []) + history if e.get('grant_fingerprint') == grant
    ]
    refresh_expiry = current.get('refresh_expires_at', current.get('refresh_token_expires_at'))
    expiry_evidence = 'provider_session_field' if refresh_expiry else 'not_recorded'
    if refresh_expiry is None:
        for event in reversed(matching):
            fields = event.get('times', {})
            refresh_expiry = fields.get(
                'refresh_expires_at',
                fields.get('refresh_token_expires_at', fields.get('refreshTokenExpiration')),
            )
            duration = fields.get('refresh_expires_in', fields.get('refresh_token_expires_in'))
            if refresh_expiry is None and duration is not None:
                refresh_expiry = event['at'] + duration
            if refresh_expiry is not None:
                expiry_evidence = 'provider_response_field'
                break
    marker = _read(
        _path(root, source, 'refresh-state' if source == 'hoopit' else 'refresh-blocked')
    )
    reason = marker.get('reason')
    tx = marker.get('transaction_id')
    if (
        reason == 'refresh-completed'
        or tx
        and reason in ('refresh-in-flight', 'refresh-response-received')
        and tx in (session.get('refresh_transaction_id'), session.get('session_transaction_id'))
    ):
        reason = None
    if reason is not None and reason not in (
        'refresh-in-flight',
        'invalid-grant',
        'refresh-rejected',
        'fatal-client-configuration',
        'refresh-response-received',
        'refresh-completed',
    ):
        reason = 'unknown' if marker else None
    blocked_at = _number(marker.get('recorded_at'))
    login_at = summary.get('last_login_observed_at')
    observed_start = login_at or summary.get('history_started_at')
    last_success = summary.get('last_successful_refresh_at')
    interval_end = (
        max(observed_start, last_success or observed_start) if observed_start is not None else None
    )
    return {
        'source': source,
        'current_times': current,
        'refresh_expires_at': refresh_expiry,
        'refresh_expiry_evidence': expiry_evidence,
        'history_started_at': summary.get('history_started_at'),
        'last_login_observed_at': login_at,
        'successful_refreshes': summary.get('successful_refreshes', 0),
        'last_failed_refresh_at': summary.get('last_failed_refresh_at'),
        'known_refresh_seconds_remaining_at_block': max(0, refresh_expiry - blocked_at)
        if refresh_expiry is not None and blocked_at is not None
        else None,
        'last_successful_refresh_at': last_success,
        'refresh_remaining_seconds': max(0, refresh_expiry - now) if refresh_expiry else None,
        'refresh_block_reason': reason,
        'saved_response_present': _path(root, source, 'refresh-response').exists()
        or reason == 'refresh-response-received',
        'observed_session_seconds': max(0, interval_end - observed_start)
        if observed_start is not None
        else None,
        'session_start_evidence': 'observed_login_or_import'
        if login_at is not None
        else 'baseline_only',
        'recent_events': [
            {k: v for k, v in e.items() if k != 'grant_fingerprint'} for e in history[-12:]
        ],
        'maximum_lifetime_verified': False,
    }


def maintenance(root, source, module, *, at=None, access_needed=False):
    """No periodic refresh unless a response is recoverable or expiry is near.

    Unknown inactivity limits never become an invented keepalive policy.
    The regular collector already refreshes access tokens on demand.
    """
    now = time.time() if at is None else at
    with private_source_lock(root, source):
        status = report(root, source, at=now)
        reason = status['refresh_block_reason']
        if reason and not status['saved_response_present']:
            return {'refreshed': False, 'reason': reason, 'requires_attention': True}
        times = status['current_times']
        access_expiry = times.get('access_expires_at', times.get('expires_at'))
        if access_expiry is None and 'issued_at' in times and 'expires_in' in times:
            access_expiry = times['issued_at'] + times['expires_in']
        due = bool(
            status['saved_response_present']
            or status['refresh_expires_at']
            and status['refresh_expires_at'] <= now + 3600
            or access_needed
            and access_expiry is not None
            and access_expiry <= now + 300
        )
        if not due:
            return {'refreshed': False, 'reason': 'not-due', 'requires_attention': False}
        # A short server TTL must not cause refresh on every five-minute check.
        if not access_needed:
            attempts = [e for e in status['recent_events'] if e['event'] == 'refresh_started']
            if attempts and now - attempts[-1]['at'] < 3600:
                return {'refreshed': False, 'reason': 'cooldown', 'requires_attention': False}
        return module.refresh_session(root, at=at)


def recovery_validation_time(response, received_at, requested_at):
    """Validate an unexpired saved response now, including legacy timestamps.

    Older journals recorded request-start rather than response receipt. A token
    issued during that request can have nbf later than the journal timestamp.
    This decode only selects the clock: signature, issuer, identity and all
    claims still must pass the provider's normal validator. Expired responses
    retain the historical recovery path and must rotate before use.
    """
    try:
        encoded = response['access_token'].split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
        expiry = claims['exp']
        if type(expiry) in (int, float) and math.isfinite(expiry) and expiry > requested_at:
            return requested_at
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    return received_at
