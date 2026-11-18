"""Fly orchestration against a fake CLI, without any account or live request."""

import io
import json
import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from familiekilder import deploy_fly as deploy
from familiekilder.hosting import INSTALLATION_ID_ENV, OWNER_CODE_HASH_ENV, OwnerCodeHash


class FakeFly:
    def __init__(self, *, existing=False, machine_count=0):
        self.existing = existing
        self.machine_count = machine_count
        self.calls = []
        self.secrets = ''
        self.config = None
        self.dockerfile = None

    def run(self, argv, **kwargs):
        args = argv[1:]
        self.calls.append(args)
        result = ''
        if args[:2] == ['apps', 'list']:
            result = json.dumps([{'Name': 'fictional-app'}] if self.existing else [])
        elif args[:2] == ['volumes', 'list']:
            result = '[]'
        elif args[:2] == ['secrets', 'list']:
            result = json.dumps([{'Name': n} for n in (INSTALLATION_ID_ENV, OWNER_CODE_HASH_ENV)] if self.existing else [])
        elif args[:2] == ['machines', 'list']:
            result = json.dumps([{'id': str(i)} for i in range(self.machine_count)])
        elif args[:2] == ['secrets', 'import']:
            self.secrets = kwargs['input']
        elif args[0] == 'deploy':
            config_path = Path(args[args.index('--config') + 1])
            self.config = config_path.read_text()
            context = Path(args[1])
            assert (context / 'Dockerfile').is_file()
            assert not list(context.rglob('.private'))
            self.check_dockerfile(args, config_path, context)
            self.machine_count = 1
        return subprocess.CompletedProcess(argv, 0, result, '')

    def check_dockerfile(self, args, config_path, context):
        """Resolve the Dockerfile the way flyctl does, while the files exist.

        flyctl joins [build] dockerfile onto the fly.toml directory even when it
        is absolute, and ignores --dockerfile when fly.toml names one.
        """
        dockerfile = tomllib.loads(self.config)['build']['dockerfile']
        assert not Path(dockerfile).is_absolute(), dockerfile
        assert not dockerfile.startswith(('/', '\\')), dockerfile
        resolved = config_path.parent / dockerfile
        assert resolved.is_file(), resolved
        assert resolved.resolve() == (context / 'Dockerfile').resolve(), resolved
        assert not any(arg == '--dockerfile' or arg.startswith('--dockerfile=') for arg in args), args
        self.dockerfile = dockerfile


class DeployTests(unittest.TestCase):
    def deploy(self, fake, directory, output, **kwargs):
        new_code = kwargs.pop('new_code', False)
        return deploy.deploy(root=Path(directory), app='fictional-app', region='arn', org='personal',
                             move=False, new_code=new_code, out=output,
                             flyctl=deploy.Flyctl('fake-flyctl', run=fake.run), **kwargs)

    def test_one_machine_mounted_disk_autostop_and_owner_code_shown_once(self):
        fake = FakeFly()
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            self.deploy(fake, directory, output, interactive=True)
        config = fake.config
        self.assertIn('auto_stop_machines = "stop"', config)
        self.assertIn('auto_start_machines = true', config)
        self.assertIn('min_machines_running = 0', config)
        self.assertIn('destination = "/data"', config)
        self.assertIn('strategy = "immediate"', config)
        self.assertEqual(fake.dockerfile, 'context/Dockerfile')
        deployment = next(args for args in fake.calls if args[0] == 'deploy')
        self.assertIn('--ha=false', deployment)
        self.assertIn('immediate', deployment)
        self.assertEqual(fake.machine_count, 1)
        self.assertEqual(sum(args[:2] == ['volumes', 'create'] for args in fake.calls), 1)
        code = output.getvalue().split('Eierkode (vises bare nå): ')[1].splitlines()[0]
        self.assertEqual(output.getvalue().count(code), 1)
        self.assertNotIn(code, json.dumps(fake.calls))
        self.assertNotIn(code, fake.secrets)
        values = dict(line.split('=', 1) for line in fake.secrets.splitlines())
        self.assertTrue(OwnerCodeHash.parse(values[OWNER_CODE_HASH_ENV]).matches(code))
        self.assertNotIn(values[INSTALLATION_ID_ENV], output.getvalue())

    def test_agent_receives_a_private_file_and_no_code_on_stdout(self):
        fake = FakeFly()
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            self.deploy(fake, directory, output, interactive=False)
            path = next((Path(directory) / '.private/remote').glob('fictional-app-eierkode-*.txt'))
            code = path.read_text().strip()
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(code, output.getvalue())
            self.assertNotIn(code, json.dumps(fake.calls))

    def test_code_is_delivered_before_secrets_even_if_deploy_fails_and_can_rotate(self):
        fake = FakeFly()
        original = fake.run
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / '.private/remote'
            def failure(argv, **kwargs):
                if argv[1:3] == ['secrets', 'import']:
                    self.assertEqual(len(list(folder.glob('*-eierkode-*.txt'))), 1)
                if argv[1] == 'deploy':
                    return subprocess.CompletedProcess(argv, 1, '', kwargs.get('input') or 'failed')
                return original(argv, **kwargs)
            fake.run = failure
            with self.assertRaises(deploy.DeployError):
                self.deploy(fake, directory, io.StringIO(), interactive=False)
            first = next(folder.glob('*-eierkode-*.txt')).read_text().strip()
            values = dict(line.split('=', 1) for line in fake.secrets.splitlines())
            self.assertTrue(OwnerCodeHash.parse(values[OWNER_CODE_HASH_ENV]).matches(first))
            # A retry discovers the staged secrets and keeps the delivered code.
            retry = FakeFly(existing=True)
            self.deploy(retry, directory, io.StringIO(), interactive=False)
            self.assertFalse(retry.secrets)
            for _ in range(2):
                self.deploy(retry, directory, io.StringIO(), interactive=False, new_code=True)
            paths = list(folder.glob('*-eierkode-*.txt'))
            self.assertEqual(len(paths), 3)
            self.assertEqual(len({path.read_text() for path in paths}), 3)

    def test_config_refuses_an_absolute_dockerfile(self):
        with self.assertRaises(ValueError):
            deploy.fly_toml('fictional-app', 'arn', '/fictional/context/Dockerfile', {})
        config = tomllib.loads(deploy.fly_toml('fictional-app', 'arn', 'context/Dockerfile',
                                               {'FAMILIEKILDER_PACKAGE': 'familiekilder-mcp==0.0.0'}))
        self.assertEqual(config['build']['dockerfile'], 'context/Dockerfile')
        self.assertEqual(config['build']['args'], {'FAMILIEKILDER_PACKAGE': 'familiekilder-mcp==0.0.0'})

    def test_word_builds_with_libreoffice_and_says_so(self):
        fake = FakeFly()
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            result = self.deploy(fake, directory, output, interactive=False, word=True)
        config = tomllib.loads(fake.config)
        self.assertEqual(config['build']['args']['WITH_LIBREOFFICE'], '1')
        self.assertEqual(config['swap_size_mb'], 512)
        self.assertIs(result['word'], True)
        self.assertIn('Word-vedlegg er på', output.getvalue())
        self.assertIn('--word også neste gang', output.getvalue())

    def test_without_word_there_is_no_libreoffice_and_the_output_says_so(self):
        fake = FakeFly()
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            result = self.deploy(fake, directory, output, interactive=False)
        config = tomllib.loads(fake.config)
        self.assertNotIn('WITH_LIBREOFFICE', config['build'].get('args', {}))
        self.assertNotIn('swap_size_mb', config)
        self.assertIs(result['word'], False)
        self.assertIn('Word-vedlegg er av', output.getvalue())

    def test_word_is_an_option_on_the_command_line(self):
        from familiekilder.source_cli import build_parser

        parser = build_parser()
        self.assertTrue(parser.parse_args(['deploy', 'fly', '--word']).word)
        self.assertFalse(parser.parse_args(['deploy', 'fly']).word)

    def test_secret_cli_errors_do_not_echo_stdin(self):
        def failed(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, '', kwargs['input'])
        with self.assertRaises(deploy.DeployError) as error:
            deploy.Flyctl('fake', run=failed)('secrets', 'import', stdin='fictional-private-value')
        self.assertNotIn('fictional-private-value', str(error.exception))

    def test_existing_id_is_preserved_and_multiple_machines_refused_before_deploy(self):
        fake = FakeFly(existing=True, machine_count=1)
        with tempfile.TemporaryDirectory() as directory:
            self.deploy(fake, directory, io.StringIO(), interactive=False)
        self.assertFalse(fake.secrets)
        fake = FakeFly(existing=True, machine_count=2)
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(deploy.DeployError):
            self.deploy(fake, directory, io.StringIO(), interactive=False)
        self.assertFalse(any(args[0] == 'deploy' for args in fake.calls))
        self.assertFalse(fake.secrets)

    def test_missing_cli_and_login_have_actionable_errors(self):
        with patch.object(deploy.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(deploy.DeployError, 'brew install flyctl'):
                deploy.find_flyctl()
        def logged_out(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1 if 'whoami' in argv else 0, '', '')
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(deploy.DeployError, 'auth login'):
            deploy.deploy(root=Path(directory), app='fictional-app', region='arn', org='personal',
                          move=False, new_code=False, flyctl=deploy.Flyctl('fake', run=logged_out))


if __name__ == '__main__':
    unittest.main()
