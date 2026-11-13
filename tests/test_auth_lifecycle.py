from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from familiekilder import (
    auth_lifecycle as life,
    vigilo_auth as vigilo,
    ist_oauth as ist,
    ist_oauth_protocol as ist_protocol,
)
from familiekilder.api_transport import ApiRequestNotSent, ApiTransportError
from familiekilder.core import FamiliekilderError
from familiekilder.private_state import atomic_write_private_json, PrivateStateError
from familiekilder_testing import ist_oauth as inf, vigilo_auth as vf


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_health_is_readonly_and_does_not_confuse_access_with_refresh(self):
        result = life.report(self.root, 'vigilo', at=100)
        self.assertFalse((self.root / '.private').exists())
        self.assertIsNone(result['refresh_expires_at'])
        life.record(self.root, 'vigilo', 'baseline', value={'expires_at': 200}, at=100)
        self.assertIsNone(life.report(self.root, 'vigilo', at=100)['refresh_expires_at'])

    def test_response_lifetime_survives_provider_field_filtering_without_secret_logging(self):
        session = {
            'refresh_token': 'sensitive-refresh',
            'access_token': 'sensitive-access',
            'expires_at': 160,
        }
        life.record(
            self.root,
            'vigilo',
            'response_received',
            at=100,
            value={
                **session,
                'refresh_expires_in': '86400',
                'username': 'private-person',
                'untrusted_field': 'private-secret',
                'scope': 'private-scope',
            },
        )
        atomic_write_private_json(life._path(self.root, 'vigilo', 'session'), session)
        result = life.report(self.root, 'vigilo', at=200)
        self.assertEqual(result['refresh_expires_at'], 86500)
        self.assertEqual(result['refresh_expiry_evidence'], 'provider_response_field')
        log = life._path(self.root, 'vigilo', 'auth-history').read_text()
        for secret in (
            'sensitive-refresh',
            'sensitive-access',
            'private-person',
            'private-secret',
            'private-scope',
        ):
            self.assertNotIn(secret, log)
            self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(
            life._path(self.root, 'vigilo', 'auth-history').stat().st_mode & 0o777, 0o600
        )

    def test_copied_installation_cannot_refresh_same_grant(self):
        original = self.root / 'original'
        copy = self.root / 'copy'
        original.mkdir()
        life.require_owner(original, 'vigilo')
        shutil.copytree(original, copy)
        with self.assertRaisesRegex(FamiliekilderError, 'annen installasjon'):
            life.require_owner(copy, 'vigilo')

    def test_owner_survives_mac_hostname_change_but_not_another_machine(self):
        with (
            patch.object(life, '_machine_identity', return_value='mac-1'),
            patch.object(life, '_hostnames', return_value={'home.lan'}),
            patch.object(life.socket, 'gethostname', return_value='home.lan'),
        ):
            life.require_owner(self.root, 'vigilo')
        with (
            patch.object(life, '_machine_identity', return_value='mac-1'),
            patch.object(life, '_hostnames', return_value={'cafe.lan'}),
            patch.object(life.socket, 'gethostname', return_value='cafe.lan'),
        ):
            life.require_owner(self.root, 'vigilo')
        with (
            patch.object(life, '_machine_identity', return_value='mac-2'),
            patch.object(life.socket, 'gethostname', return_value='home.lan'),
        ):
            with self.assertRaisesRegex(FamiliekilderError, 'annen installasjon'):
                life.require_owner(self.root, 'vigilo')

    def test_legacy_owner_upgrades_through_stable_mac_name_only(self):
        owner = life._path(self.root, 'vigilo', 'auth-owner')
        legacy = {
            'root': str(self.root.resolve()),
            'host': life._owner_digest('MacBook-Pro-19.local'),
        }
        life.atomic_write_private_json(owner, legacy)
        with (
            patch.object(life, '_machine_identity', return_value='mac-1'),
            patch.object(life, '_hostnames', return_value={'other.lan'}),
        ):
            with self.assertRaisesRegex(FamiliekilderError, 'annen installasjon'):
                life.require_owner(self.root, 'vigilo')
        with (
            patch.object(life, '_machine_identity', return_value='mac-1'),
            patch.object(life, '_hostnames', return_value={'Mac.lan', 'MacBook-Pro-19.local'}),
        ):
            life.require_owner(self.root, 'vigilo')
        upgraded = life._read(owner)
        self.assertEqual(upgraded['host'], legacy['host'])
        self.assertEqual(upgraded['machine'], life._owner_digest('mac-1'))

    def test_hardware_bound_owner_never_falls_back_to_hostname(self):
        with (
            patch.object(life, '_machine_identity', return_value='mac-1'),
            patch.object(life.socket, 'gethostname', return_value='same-name'),
        ):
            life.require_owner(self.root, 'vigilo')
        # Covers both a copy on another platform and a failed hardware lookup.
        with (
            patch.object(life, '_machine_identity', return_value=None),
            patch.object(life, '_hostnames', return_value={'same-name'}),
            patch.object(life.socket, 'gethostname', return_value='same-name'),
        ):
            with self.assertRaisesRegex(FamiliekilderError, 'annen installasjon'):
                life.require_owner(self.root, 'vigilo')

    def test_owner_on_other_platforms_stays_hostname_bound(self):
        with (
            patch.object(life.sys, 'platform', 'linux'),
            patch.object(life.socket, 'gethostname', return_value='vps'),
        ):
            life.require_owner(self.root, 'vigilo')
            self.assertNotIn('machine', life._read(life._path(self.root, 'vigilo', 'auth-owner')))
        with (
            patch.object(life.sys, 'platform', 'linux'),
            patch.object(life.socket, 'gethostname', return_value='clone'),
        ):
            with self.assertRaisesRegex(FamiliekilderError, 'annen installasjon'):
                life.require_owner(self.root, 'vigilo')

    def test_journal_is_bound_to_both_session_and_transaction(self):
        session = {'refresh_token': 'old'}
        life.save_response(self.root, 'vigilo', session, 'tx', {'refresh_token': 'new'}, at=100)
        self.assertIsNone(life.saved_response(self.root, 'vigilo', session, 'other-tx'))
        with self.assertRaises(FamiliekilderError):
            life.saved_response(self.root, 'vigilo', {'refresh_token': 'other'}, 'tx')
        self.assertEqual(
            life.saved_response(self.root, 'vigilo', session, 'tx')['response']['refresh_token'],
            'new',
        )

    def test_maintenance_ignores_access_expiry_until_access_needed(self):
        from unittest.mock import Mock

        module = Mock()
        atomic_write_private_json(
            life._path(self.root, 'spond', 'session'),
            {'access_expires_at': 99, 'refresh_expires_at': 90000},
        )
        self.assertEqual(life.maintenance(self.root, 'spond', module, at=100)['reason'], 'not-due')
        module.refresh_session.assert_not_called()
        life.maintenance(self.root, 'spond', module, at=100, access_needed=True)
        module.refresh_session.assert_called_once()

    def test_maintenance_only_near_known_refresh_expiry(self):
        from unittest.mock import Mock

        module = Mock()
        atomic_write_private_json(
            life._path(self.root, 'spond', 'session'), {'refresh_expires_at': 3700}
        )
        life.maintenance(self.root, 'spond', module, at=100)
        module.refresh_session.assert_called_once()
        module.reset_mock()
        life.record(self.root, 'spond', 'refresh_started', at=100)
        self.assertEqual(life.maintenance(self.root, 'spond', module, at=101)['reason'], 'cooldown')
        module.refresh_session.assert_not_called()

    def test_unknown_legacy_marker_never_causes_replay(self):
        from unittest.mock import Mock

        module = Mock()
        atomic_write_private_json(
            life._path(self.root, 'vigilo', 'refresh-blocked'), {'reason': 'refresh-in-flight'}
        )
        result = life.maintenance(self.root, 'vigilo', module, at=100)
        self.assertTrue(result['requires_attention'])
        module.refresh_session.assert_not_called()

    def test_response_for_old_grant_does_not_invent_new_grant_expiry(self):
        life.record(
            self.root,
            'vigilo',
            'response_received',
            value={'refresh_token': 'old', 'refresh_expires_in': 1000},
            at=100,
        )
        atomic_write_private_json(
            life._path(self.root, 'vigilo', 'session'), {'refresh_token': 'new'}
        )
        self.assertIsNone(life.report(self.root, 'vigilo')['refresh_expires_at'])


class VigiloRecoveryTests(unittest.TestCase):
    setUp = vf.VigiloAuthFixture.setUp
    configure = vf.VigiloAuthFixture.configure
    login = vf.VigiloAuthFixture.login

    def seed(self):
        self.configure()
        self.login(
            vf.FakeAuthTransport([{'access_token': 'access-A', 'refresh_token': 'refresh-A'}])
        )

    def test_validation_interruption_recovers_received_response_without_second_post(self):
        self.seed()
        first = vf.FakeAuthTransport(
            [
                {
                    'access_token': 'access-B',
                    'refresh_token': 'refresh-B',
                    'refresh_expires_in': 7776000,
                }
            ]
        )

        def crash(*args):
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            vigilo.refresh_session(
                self.root, transport=first, access_token_validator=crash, at=1_800_000_100
            )
        second = vf.FakeAuthTransport([])
        result = vigilo.refresh_session(
            self.root, transport=second, access_token_validator=vf._validator, at=1_800_000_120
        )
        self.assertTrue(result['refreshed'])
        self.assertEqual(second.posts, [])
        self.assertEqual(vigilo.local_session_for_use(self.root)['refresh_token'], 'refresh-B')
        self.assertEqual(
            life.report(self.root, 'vigilo')['refresh_expiry_evidence'], 'provider_response_field'
        )
        self.assertFalse(life._path(self.root, 'vigilo', 'refresh-response').exists())

    def test_vigilo_receipt_clock_is_after_token_exchange(self):
        self.seed()
        clock = [1_800_000_100.9]
        transport = vf.FakeAuthTransport([{'access_token': 'B', 'refresh_token': 'refresh-B'}])
        original = transport.post_form

        def post(*args, **kwargs):
            value = original(*args, **kwargs)
            clock[0] += 0.2
            return value

        observed = []

        def validator(*args):
            observed.append(args[-1])
            claims = dict(vf._validator(*args))
            claims['nbf'] = 1_800_000_101
            return claims

        with (
            patch.object(vigilo.time, 'time', side_effect=lambda: clock[0]),
            patch.object(transport, 'post_form', side_effect=post),
        ):
            self.assertTrue(
                vigilo.refresh_session(
                    self.root, transport=transport, access_token_validator=validator
                )['refreshed']
            )
        self.assertGreaterEqual(observed[0], 1_800_000_101)

    def _claims_with_nbf(self, not_before):
        def validator(*args):
            return {**vf._validator(*args), 'nbf': not_before}

        return validator

    def test_response_stamped_milliseconds_ahead_does_not_block_grant(self):
        self.seed()
        transport = vf.FakeAuthTransport([{'access_token': 'B', 'refresh_token': 'refresh-B'}])
        result = vigilo.refresh_session(
            self.root,
            transport=transport,
            at=1_800_000_100.987,
            access_token_validator=self._claims_with_nbf(1_800_000_101),
        )
        self.assertTrue(result['refreshed'])
        self.assertFalse(vigilo.auth_status(self.root)['refresh_blocked'])

    def test_token_outside_clock_tolerance_is_still_rejected(self):
        self.seed()
        transport = vf.FakeAuthTransport([{'access_token': 'B', 'refresh_token': 'refresh-B'}])
        with self.assertRaises(vigilo.VigiloRefreshUncertain):
            vigilo.refresh_session(
                self.root,
                transport=transport,
                at=1_800_000_100,
                access_token_validator=self._claims_with_nbf(
                    1_800_000_100 + life.NOT_BEFORE_LEEWAY_SECONDS + 1
                ),
            )
        self.assertTrue(vigilo.auth_status(self.root)['refresh_blocked'])

    def test_saved_response_blocked_by_clock_recovers_days_later_with_its_new_token(self):
        self.seed()
        received_at = 1_800_000_100.987
        first = vf.FakeAuthTransport([{'access_token': 'access-B', 'refresh_token': 'refresh-B'}])

        def crash(*args):
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            vigilo.refresh_session(
                self.root, transport=first, access_token_validator=crash, at=received_at
            )

        def validator(token, jwks, config, now):
            if token != 'access-B':
                return vf._validator(token, jwks, config, now)
            return {**vf._claims(exp=received_at + 600), 'nbf': received_at + 0.013}

        second = vf.FakeAuthTransport([{'access_token': 'access-C', 'refresh_token': 'refresh-C'}])
        result = vigilo.refresh_session(
            self.root,
            transport=second,
            access_token_validator=validator,
            at=received_at + 18 * 86400,
        )
        self.assertTrue(result['refreshed'])
        self.assertEqual([post[1]['refresh_token'] for post in second.posts], ['refresh-B'])
        self.assertEqual(vigilo.local_session_for_use(self.root)['refresh_token'], 'refresh-C')

    def test_definitely_unsent_does_not_permanently_block_grant(self):
        self.seed()
        with self.assertRaises(vigilo.VigiloRefreshUncertain):
            vigilo.refresh_session(
                self.root,
                transport=vf.FakeAuthTransport([ApiRequestNotSent('not sent')]),
                at=1_800_000_100,
            )
        self.assertFalse(vigilo.auth_status(self.root)['refresh_blocked'])
        self.assertEqual(vigilo.local_session_for_use(self.root)['refresh_token'], 'refresh-A')

    def test_ambiguous_transport_remains_blocked(self):
        self.seed()
        first = vf.FakeAuthTransport([ApiTransportError('unknown')])
        with self.assertRaises(vigilo.VigiloRefreshUncertain):
            vigilo.refresh_session(self.root, transport=first, at=1_800_000_100)
        second = vf.FakeAuthTransport([])
        with self.assertRaises(vigilo.VigiloRefreshUncertain):
            vigilo.refresh_session(self.root, transport=second, at=1_800_000_120)
        self.assertEqual(second.posts, [])

    def test_session_write_failure_preserves_reply_for_restart(self):
        self.seed()
        original = vigilo.atomic_write_private_json

        def fail_session(path, value, **kwargs):
            if path == vigilo.session_path(self.root):
                raise PrivateStateError('write failed')
            return original(path, value, **kwargs)

        with patch.object(vigilo, 'atomic_write_private_json', side_effect=fail_session):
            with self.assertRaises(vigilo.VigiloRefreshUncertain):
                vigilo.refresh_session(
                    self.root,
                    transport=vf.FakeAuthTransport(
                        [{'access_token': 'B', 'refresh_token': 'refresh-B'}]
                    ),
                    access_token_validator=vf._validator,
                    at=1_800_000_100,
                )
        second = vf.FakeAuthTransport([])
        vigilo.refresh_session(
            self.root, transport=second, access_token_validator=vf._validator, at=1_800_000_120
        )
        self.assertEqual(second.posts, [])


class IstRecoveryTests(unittest.TestCase):
    setUp = inf.IstOAuthFixture.setUp
    tearDown = inf.IstOAuthFixture.tearDown
    import_synthetic = inf.IstOAuthFixture.import_synthetic
    access_claims_validator = inf.IstOAuthFixture.access_claims_validator

    def test_validation_crash_recovers_after_restart_without_post(self):
        self.import_synthetic(inf._session(issued_at=1_999_999_000))

        def crash(*args):
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            ist.refresh_session(
                self.root,
                transport=inf.FakeTransport(),
                access_token_validator=crash,
                at=2_000_000_000,
            )
        second = inf.FakeTransport()
        result = ist.refresh_session(
            self.root,
            transport=second,
            access_token_validator=self.access_claims_validator,
            at=2_000_000_020,
        )
        self.assertTrue(result['refreshed'])
        self.assertFalse(any(c[0] == 'post_form' for c in second.calls))
        self.assertEqual(
            ist.local_session_for_use(self.root)['refresh_token'], 'new-synthetic-refresh-token'
        )

    def test_receipt_clock_accounts_for_token_issued_during_http_request(self):
        self.import_synthetic(inf._session(issued_at=1_999_999_000))
        clock = [2_000_000_000.9]
        transport = inf.FakeTransport()
        original = transport.post_form

        def post(*args, **kwargs):
            value = original(*args, **kwargs)
            clock[0] += 0.2
            return value

        observed = []

        def validator(token, jwks, config, now):
            observed.append(now)
            claims = dict(self.access_claims_validator(token, jwks, config, now))
            claims['nbf'] = 2_000_000_001
            return claims

        with (
            patch.object(ist.time, 'time', side_effect=lambda: clock[0]),
            patch.object(transport, 'post_form', side_effect=post),
        ):
            result = ist.refresh_session(
                self.root, transport=transport, access_token_validator=validator
            )
        self.assertTrue(result['refreshed'])
        self.assertGreaterEqual(observed[0], 2_000_000_001)

    def test_legacy_start_timestamp_recovers_without_reposting_or_ignoring_nbf(self):
        self.import_synthetic(inf._session(issued_at=1_999_999_000))
        not_before = 2_000_000_000 + life.NOT_BEFORE_LEEWAY_SECONDS + 1

        def validator(token, jwks, config, now):
            claims = dict(self.access_claims_validator(token, jwks, config, now))
            claims['nbf'] = not_before
            return claims

        with self.assertRaises(ist.OAuthRefreshUncertain):
            ist.refresh_session(
                self.root,
                transport=inf.FakeTransport(),
                access_token_validator=validator,
                at=2_000_000_000,
            )
        second = inf.FakeTransport()
        # Recovery still rejects a token beyond clock tolerance at the current time.
        with self.assertRaises(ist.OAuthRefreshUncertain):
            ist.refresh_session(
                self.root, transport=second, access_token_validator=validator, at=2_000_000_000.05
            )
        result = ist.refresh_session(
            self.root, transport=second, access_token_validator=validator, at=not_before + 1
        )
        self.assertTrue(result['refreshed'])
        self.assertFalse(any(c[0] == 'post_form' for c in second.calls))

    def test_response_stamped_milliseconds_ahead_does_not_block_ist(self):
        self.import_synthetic(inf._session(issued_at=1_999_999_000))

        def validator(token, jwks, config, now):
            claims = dict(self.access_claims_validator(token, jwks, config, now))
            claims['nbf'] = now + 0.067
            return claims

        result = ist.refresh_session(
            self.root,
            transport=inf.FakeTransport(),
            access_token_validator=validator,
            at=2_000_000_000,
        )
        self.assertTrue(result['refreshed'])
        self.assertFalse(ist.auth_status(self.root)['refresh_blocked'])

    def test_definitely_unsent_does_not_block_ist(self):
        self.import_synthetic(inf._session(issued_at=1_999_999_000))
        transport = inf.FakeTransport()
        with patch.object(transport, 'post_form', side_effect=ApiRequestNotSent('not sent')):
            with self.assertRaises(ist.OAuthRefreshUncertain):
                ist.refresh_session(self.root, transport=transport, at=2_000_000_000)
        self.assertFalse(ist.auth_status(self.root)['refresh_blocked'])

    def test_expired_received_access_recovers_new_refresh_before_next_post(self):
        self.import_synthetic(inf._session(issued_at=1_999_999_000))
        first = inf.FakeTransport()

        def crash(*args):
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            ist.refresh_session(
                self.root, transport=first, access_token_validator=crash, at=2_000_000_000
            )
        second = inf.FakeTransport()

        def validator(token, jwks, config, now):
            claims = dict(self.access_claims_validator(token, jwks, config, now))
            claims['exp'] = now + 3600
            return claims

        ist.refresh_session(
            self.root, transport=second, access_token_validator=validator, at=2_000_020_000
        )
        posts = [c for c in second.calls if c[0] == 'post_form']
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2]['refresh_token'], 'new-synthetic-refresh-token')


class OwnershipConcurrencyTests(unittest.TestCase):
    def test_second_thread_cannot_refresh_same_source(self):
        import threading
        from familiekilder.private_lock import PrivateLockBusy

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entered, release = threading.Event(), threading.Event()
            errors = []

            @life.operation('vigilo')
            def refresh(root):
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('test timeout')
                return {'refreshed': True}

            def first():
                try:
                    refresh(root)
                except Exception as exc:
                    errors.append(type(exc).__name__)

            worker = threading.Thread(target=first)
            worker.start()
            self.assertTrue(entered.wait(3))
            try:
                with self.assertRaises(PrivateLockBusy):
                    refresh(root)
            finally:
                release.set()
                worker.join(3)
            self.assertEqual(errors, [])


from familiekilder_testing import spond_auth as sf
from familiekilder import spond_auth as spond


class SpondRecoveryTests(unittest.TestCase):
    setUp = sf.SpondAuthFixture.setUp
    _store_provider = sf.SpondAuthFixture._store_provider
    _store_session = sf.SpondAuthFixture._store_session

    def test_restart_after_rotated_reply_does_not_replay_old_refresh(self):
        self._store_session()
        first = sf.FakeAuthTransport()
        with patch.object(spond, '_rotated_tokens', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                spond.refresh_session(
                    self.root, transport=first, at=sf.NOW, before_request=lambda: None
                )
        second = sf.FakeAuthTransport()
        result = spond.refresh_session(
            self.root, transport=second, at=sf.NOW + 20, before_request=lambda: None
        )
        self.assertTrue(result['refreshed'])
        self.assertEqual(second.posts, [])
        self.assertEqual(
            spond.local_session_for_use(self.root, at=sf.NOW + 20)['refresh_token'],
            'rotated-refresh-secret',
        )


class BoundedHistoryTests(unittest.TestCase):
    setUp = LifecycleTests.setUp

    def test_rotation_preserves_totals_login_and_refresh_expiry_evidence(self):
        session = {'refresh_token': 'current-grant'}
        atomic_write_private_json(life._path(self.root, 'spond', 'session'), session)
        life.record(self.root, 'spond', 'login_succeeded', at=100)
        life.record(
            self.root,
            'spond',
            'response_received',
            at=101,
            value=session | {'refresh_expires_in': 100000},
        )
        with patch.object(life, 'HISTORY_LIMIT', 4):
            for instant in range(102, 118):
                life.record(self.root, 'spond', 'refresh_succeeded', value=session, at=instant)
        history = life._read(life._path(self.root, 'spond', 'auth-history'))
        self.assertEqual(len(history['events']), 4)
        report = life.report(self.root, 'spond', at=118)
        self.assertEqual(report['successful_refreshes'], 16)
        self.assertEqual(report['history_started_at'], 100)
        self.assertEqual(report['last_login_observed_at'], 100)
        self.assertEqual(report['last_successful_refresh_at'], 117)
        self.assertEqual(report['refresh_expires_at'], 100101)
        self.assertEqual(report['observed_session_seconds'], 17)
        self.assertNotIn(
            'current-grant', life._path(self.root, 'spond', 'auth-history').read_text()
        )

    def test_corrupt_history_cannot_block_operation_or_hide_its_error(self):
        atomic_write_private_json(
            life._path(self.root, 'spond', 'auth-history'),
            {'events': 'invalid', 'summary': 'invalid'},
        )

        @life.operation('spond')
        def success(root):
            return 'done'

        self.assertEqual(success(self.root), 'done')
        self.assertEqual(life.report(self.root, 'spond')['successful_refreshes'], 1)
        original = life.atomic_write_private_json

        def fail_history(path, value):
            if path.name.endswith('auth-history.json'):
                raise PrivateStateError('diagnostic write failed')
            return original(path, value)

        @life.operation('spond')
        def failed(root):
            raise ValueError('original error')

        with patch.object(life, 'atomic_write_private_json', side_effect=fail_history):
            self.assertEqual(success(self.root), 'done')
            with self.assertRaisesRegex(ValueError, 'original error'):
                failed(self.root)
            life.save_response(
                self.root, 'spond', {'refresh_token': 'old'}, 'tx', {'refresh_token': 'new'}, at=100
            )
        self.assertIsNotNone(
            life.saved_response(self.root, 'spond', {'refresh_token': 'old'}, 'tx')
        )

    def test_diagnostic_isolation_does_not_suppress_credential_journal_errors(self):
        with patch.object(
            life, 'atomic_write_private_json', side_effect=PrivateStateError('journal write failed')
        ):
            with self.assertRaisesRegex(PrivateStateError, 'journal write failed'):
                life.save_response(
                    self.root,
                    'spond',
                    {'refresh_token': 'old'},
                    'tx',
                    {'refresh_token': 'new'},
                    at=100,
                )
        atomic_write_private_json(
            life._path(self.root, 'spond', 'auth-history'),
            {'events': [{'at': 100, 'event': []}, None], 'summary': {'grant_evidence': []}},
        )
        self.assertEqual(life.report(self.root, 'spond')['recent_events'], [])

    def test_response_diagnostic_decode_failure_does_not_discard_rotated_reply(self):
        from unittest.mock import Mock

        response = {'access_token': 'new-access', 'refresh_token': 'new-refresh'}
        client = Mock()
        client.post_form.return_value = response
        observed = life.observe_transport(self.root, 'spond', client, at=100)
        with patch.object(
            life, 'time_metadata', side_effect=RecursionError('malformed diagnostic claims')
        ):
            self.assertIs(observed.post_form('synthetic', {}), response)
        client.post_form.assert_called_once()

    def test_diagnostic_session_decode_cannot_prevent_replacement_login(self):
        original_read = life._read

        def read(path):
            if path.name == 'spond-session.json':
                raise RecursionError('malformed diagnostic session')
            return original_read(path)

        @life.operation('spond', 'login')
        def replace(root):
            return 'replacement committed'

        with patch.object(life, '_read', side_effect=read):
            self.assertEqual(replace(self.root), 'replacement committed')

    def test_oversized_legacy_history_is_replaced_without_breaking_auth(self):
        path = life._path(self.root, 'spond', 'auth-history')
        atomic_write_private_json(path, {'events': []})
        with path.open('ab') as stream:
            stream.write(b' ' * (16 * 1024 * 1024))
        life.record(self.root, 'spond', 'refresh_succeeded', at=100)
        self.assertLess(path.stat().st_size, 4096)
        self.assertEqual(life.report(self.root, 'spond')['successful_refreshes'], 1)

    def test_legacy_journal_upgrades_only_after_exact_binding_is_verified(self):
        session = {'refresh_token': 'old', 'profile_id': 'user', 'app_version': 'old-version'}
        path = life._path(self.root, 'spond', 'refresh-response')
        atomic_write_private_json(
            path,
            {
                'session_binding': life._digest(session),
                'transaction_id': 'tx',
                'received_at': 100,
                'response': {'refresh_token': 'new'},
            },
        )
        with self.assertRaises(FamiliekilderError):
            life.saved_response(self.root, 'spond', session | {'app_version': 'changed'}, 'tx')
        self.assertEqual(
            life.saved_response(self.root, 'spond', session, 'tx')['binding_version'], 2
        )
        changed = session | {'app_version': 'changed', 'attachment_hosts': ['new.example']}
        self.assertIsNotNone(life.saved_response(self.root, 'spond', changed, 'tx'))
        for field in ('refresh_token', 'profile_id'):
            with self.assertRaises(FamiliekilderError):
                life.saved_response(self.root, 'spond', changed | {field: 'different'}, 'tx')


class SpondMetadataRecoveryTests(unittest.TestCase):
    setUp = sf.SpondAuthFixture.setUp
    _store_provider = sf.SpondAuthFixture._store_provider
    _store_session = sf.SpondAuthFixture._store_session

    def test_saved_rotated_reply_recovers_after_provider_metadata_update(self):
        self._store_session()
        with patch.object(spond, '_rotated_tokens', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                spond.refresh_session(
                    self.root,
                    transport=sf.FakeAuthTransport(),
                    at=sf.NOW,
                    before_request=lambda: None,
                )
        self._store_provider(app_version='4.63.0', attachment_hosts=['new.example.test'])
        client = sf.FakeAuthTransport()
        result = spond.refresh_session(
            self.root, transport=client, at=sf.NOW + 20, before_request=lambda: None
        )
        self.assertTrue(result['refreshed'])
        self.assertEqual(client.posts, [])
        session = spond.local_session_for_use(self.root, at=sf.NOW + 20)
        self.assertEqual(session['refresh_token'], 'rotated-refresh-secret')
        self.assertEqual(session['app_version'], '4.63.0')


class SignedTokenClockTests(unittest.TestCase):
    """Real signatures: PyJWT checks iat itself, before our own claim checks."""

    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key()))
        cls.jwks = {'keys': [{**public, 'kid': 'fixture-key', 'use': 'sig', 'alg': 'RS256'}]}

    def sign(self, claims, *, ahead):
        now = time.time()
        stamp = int(now) + ahead
        token = jwt.encode(
            {**claims, 'iat': stamp, 'nbf': stamp, 'exp': int(now) + 3600},
            self.key,
            algorithm='RS256',
            headers={'kid': 'fixture-key'},
        )
        return token, now

    def vigilo(self, ahead):
        config = vigilo._parse_client_config(vf._config())
        token, now = self.sign(vf._claims(exp=1), ahead=ahead)
        return vigilo._pyjwt_access_token_validator(token, self.jwks, config, now)

    def ist_config(self):
        return ist.ClientConfig(
            ist.AUTHORITY, inf.TEST_CLIENT_ID, 'secret', ist.REDIRECT_URI, inf.SCOPE
        )

    def ist_id(self, ahead):
        claims = {
            'iss': ist.AUTHORITY,
            'aud': inf.TEST_CLIENT_ID,
            'azp': inf.TEST_CLIENT_ID,
            'sub': 'subject-1',
            'nonce': 'n',
        }
        token, now = self.sign(claims, ahead=ahead)
        return ist_protocol._pyjwt_token_validator(token, self.jwks, self.ist_config(), 'n', now)

    def ist_access(self, ahead):
        audience = ist_protocol.ACCESS_TOKEN_AUDIENCE
        claims = {
            'iss': ist.AUTHORITY,
            'aud': audience,
            'sub': 'subject-1',
            'client_id': inf.TEST_CLIENT_ID,
            'scope': audience,
        }
        token, now = self.sign(claims, ahead=ahead)
        return ist_protocol._pyjwt_access_token_validator(token, self.jwks, self.ist_config(), now)

    def test_small_clock_skew_passes_real_signed_validation(self):
        for validate in (self.vigilo, self.ist_id, self.ist_access):
            with self.subTest(validate.__name__):
                self.assertEqual(validate(5)['sub'], 'subject-1')

    def test_token_beyond_tolerance_is_rejected_by_real_signed_validation(self):
        ahead = life.NOT_BEFORE_LEEWAY_SECONDS + 30
        for validate in (self.vigilo, self.ist_id, self.ist_access):
            with self.subTest(validate.__name__):
                with self.assertRaisesRegex(
                    FamiliekilderError, 'signatur|ikke gyldig ennå|JWKS|verifiser'
                ):
                    validate(ahead)
