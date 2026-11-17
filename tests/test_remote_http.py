"""HTTP mode with the built-in OAuth server, against a real server on loopback."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from familiekilder import remote_auth
from familiekilder.hosting import HostingError, host_settings
from familiekilder.core import FamiliekilderError
from familiekilder.live_config import Config
from familiekilder.remote_http import build_http_app
from familiekilder.source_server import ReadOnlyRuntime
from familiekilder_testing import remote as fake

ITEM = {
    "source": "spond",
    "source_id": "post:oppdiktet",
    "title": "Oppdiktet trening",
    "body": "Ta med vannflaske.",
    "reference": {"kind": "post", "id": "oppdiktet"},
    "attachments": [],
}


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


class Host:
    """One HTTP host with a fictional Spond source, started per test."""

    def __init__(self, test: unittest.TestCase) -> None:
        directory = tempfile.TemporaryDirectory()
        test.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.port = fake.free_port()
        environment = patch.dict(os.environ, fake.host_environment(self.port))
        environment.start()
        test.addCleanup(environment.stop)
        Config(self.root, {"spond": frozenset({"messages"})}).save()
        config = Config.load(self.root)
        self.runtime = ReadOnlyRuntime(
            config, reader=lambda *_: ([dict(ITEM)], True), getter=lambda *_: dict(ITEM)
        )
        test.addCleanup(self.runtime.close)
        self.built = build_http_app(config, host_settings(), runtime=self.runtime)
        self.context = fake.running(self.built.app, self.port)
        self.base = self.context.__enter__()
        test.addCleanup(self.context.__exit__, None, None, None)

    def approve(self, client_id: str, *, challenge: str, redirect_uri=fake.REDIRECT_URI,
                owner_code=fake.OWNER_CODE, **extra) -> httpx.Response:
        query = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "oppdiktet-tilstand",
            "resource": self.base + "/mcp",
            "scope": "familiekilder offline_access",
            **extra,
        }
        response = httpx.get(self.base + "/authorize?" + urlencode(query))
        if response.status_code != 302 or "/godkjenn" not in response.headers.get("location", ""):
            return response
        page = response.headers["location"]
        request_id = parse_qs(urlsplit(page).query)["foresporsel"][0]
        return httpx.post(
            self.base + "/godkjenn",
            data={"foresporsel": request_id, "eierkode": owner_code, "valg": "godkjenn"},
        )

    def code(self, client_id: str, challenge: str) -> str:
        answer = self.approve(client_id, challenge=challenge)
        assert answer.status_code == 303, answer.text
        query = parse_qs(urlsplit(answer.headers["location"]).query)
        assert query["iss"] == [self.base]
        assert query["state"] == ["oppdiktet-tilstand"]
        return query["code"][0]

    def token(self, **form) -> httpx.Response:
        return httpx.post(self.base + "/token", data=form)

    def tokens(self, client_id: str) -> dict:
        verifier, challenge = _pkce()
        code = self.code(client_id, challenge)
        response = self.token(
            grant_type="authorization_code", code=code, redirect_uri=fake.REDIRECT_URI,
            client_id=client_id, code_verifier=verifier, resource=self.base + "/mcp",
        )
        assert response.status_code == 200, response.text
        return response.json()

    def call(self, access_token: str | None) -> httpx.Response:
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if access_token:
            headers["Authorization"] = "Bearer " + access_token
        return httpx.post(
            self.base + "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )


class RemoteHttpTests(unittest.TestCase):
    def test_oversized_registration_cannot_break_an_existing_connection(self) -> None:
        host = Host(self)
        client = fake.register(host.base)['client_id']
        issued = host.tokens(client)
        path = host.built.provider.store.path
        original = path.read_bytes()
        for _ in range(3):
            refused = httpx.post(host.base + '/register', json={
                'redirect_uris': [fake.REDIRECT_URI],
                'client_name': 'x' * (3 * 1024 * 1024),
            })
            self.assertEqual(refused.status_code, 413)
        bounded = fake.register(host.base, client_name='x' * 257)
        self.assertEqual(bounded['status'], 400)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(host.call(issued['access_token']).status_code, 200)

    def test_store_size_guard_keeps_the_last_readable_file_for_every_transaction(self) -> None:
        host = Host(self)
        client = fake.register(host.base)['client_id']
        issued = host.tokens(client)
        store = host.built.provider.store
        original = store.path.read_bytes()
        with patch.object(remote_auth, 'MAX_OAUTH_STORE_BYTES', len(original) + 64):
            with self.assertRaisesRegex(FamiliekilderError, 'fullt'):
                store.transaction(lambda value: value.update(oversized='x' * 1024))
        self.assertEqual(store.path.read_bytes(), original)
        self.assertEqual(host.call(issued['access_token']).status_code, 200)

    def test_full_flow_with_the_sdk_client_lists_and_calls_tools(self) -> None:
        host = Host(self)

        async def flow():
            async with fake.session(host.base) as connected:
                tools = await connected.list_tools()
                listed = await connected.call_tool("spond_list_items", {})
                return tools, listed

        tools, listed = asyncio.run(flow())
        names = [tool.name for tool in tools.tools]
        self.assertIn("family_connections", names)
        self.assertIn("spond_list_items", names)
        self.assertNotIn("spond_send_message", names)
        self.assertTrue(all(tool.annotations.read_only_hint for tool in tools.tools))
        self.assertEqual(json.loads(listed.content[0].text)["items"][0]["title"], ITEM["title"])

    def test_without_token_the_server_answers_401_pointing_to_resource_metadata(self) -> None:
        host = Host(self)
        response = host.call(None)
        self.assertEqual(response.status_code, 401)
        header = response.headers["www-authenticate"]
        self.assertIn(f'resource_metadata="{host.base}/.well-known/oauth-protected-resource/mcp"', header)
        resource = httpx.get(host.base + "/.well-known/oauth-protected-resource/mcp").json()
        self.assertEqual(resource["resource"], host.base + "/mcp")
        self.assertEqual(resource["authorization_servers"], [host.base])
        self.assertEqual(httpx.get(host.base + "/.well-known/oauth-protected-resource").json(), resource)
        metadata = httpx.get(host.base + "/.well-known/oauth-authorization-server").json()
        self.assertEqual(metadata["issuer"], host.base)
        self.assertEqual(metadata["code_challenge_methods_supported"], ["S256"])
        self.assertEqual(metadata["registration_endpoint"], host.base + "/register")
        self.assertIn("refresh_token", metadata["grant_types_supported"])
        self.assertIn("offline_access", metadata["scopes_supported"])
        self.assertIn("none", metadata["token_endpoint_auth_methods_supported"])
        self.assertTrue(metadata["authorization_response_iss_parameter_supported"])
        self.assertEqual(httpx.get(host.base + "/healthz").text, "ok")

    def test_wrong_owner_code_is_refused_and_throttled_across_restarts(self) -> None:
        host = Host(self)
        client = fake.register(host.base)["client_id"]
        _, challenge = _pkce()
        for _ in range(remote_auth.ATTEMPT_LIMITS[0][1]):
            answer = host.approve(client, challenge=challenge, owner_code="feil-kode-0000")
            self.assertEqual(answer.status_code, 401)
            self.assertIn("Feil eierkode", answer.text)
            self.assertNotIn("code=", answer.headers.get("location", ""))
        blocked = host.approve(client, challenge=challenge)
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("Retry-After", blocked.headers)
        # A new process reads the same counter from disk.
        gate = remote_auth.OwnerCodeGate(host.root, host_settings())
        with self.assertRaises(remote_auth.OwnerCodeLimited):
            gate.check(fake.OWNER_CODE)
        later = remote_auth.OwnerCodeGate(host.root, host_settings(), clock=lambda: time.time() + 16 * 60)
        self.assertTrue(later.check(fake.OWNER_CODE))

    def test_pkce_is_required_and_checked(self) -> None:
        host = Host(self)
        client = fake.register(host.base)["client_id"]
        missing = httpx.get(host.base + "/authorize?" + urlencode({
            "response_type": "code", "client_id": client, "redirect_uri": fake.REDIRECT_URI,
            "state": "s",
        }), follow_redirects=False)
        self.assertNotIn("/godkjenn", missing.headers.get("location", ""))
        self.assertIn("error=invalid_request", missing.headers.get("location", "") + missing.text)
        plain = host.approve(client, challenge="abc", code_challenge_method="plain")
        self.assertNotEqual(plain.status_code, 303)
        _, challenge = _pkce()
        code = host.code(client, challenge)
        wrong = host.token(
            grant_type="authorization_code", code=code, redirect_uri=fake.REDIRECT_URI,
            client_id=client, code_verifier=secrets.token_urlsafe(48),
        )
        self.assertEqual(wrong.status_code, 400)
        self.assertEqual(wrong.json()["error"], "invalid_grant")

    def test_redirect_uri_must_match_exactly(self) -> None:
        host = Host(self)
        refused = fake.register(host.base, "https://angriper.example/callback")
        self.assertEqual(refused["status"], 400)
        self.assertEqual(refused["error"], "invalid_redirect_uri")
        client = fake.register(host.base)["client_id"]
        _, challenge = _pkce()
        for other in (
            "http://localhost:3030/callback/",
            "http://localhost:3030/callback?x=1",
            "http://localhost:3031/callback",
        ):
            with self.subTest(redirect=other):
                answer = host.approve(client, challenge=challenge, redirect_uri=other)
                self.assertEqual(answer.status_code, 400)
                self.assertNotIn("location", answer.headers)
        verifier, challenge = _pkce()
        code = host.code(client, challenge)
        switched = host.token(
            grant_type="authorization_code", code=code, redirect_uri="http://localhost:3030/annen",
            client_id=client, code_verifier=verifier,
        )
        self.assertEqual(switched.status_code, 400)

    def test_access_tokens_expire_and_can_be_revoked(self) -> None:
        host = Host(self)
        client = fake.register(host.base)["client_id"]
        issued = host.tokens(client)
        self.assertEqual(host.call(issued["access_token"]).status_code, 200)
        store = host.built.provider.store
        with patch.object(store, "clock", lambda: time.time() + remote_auth.ACCESS_TOKEN_SECONDS + 1):
            self.assertEqual(host.call(issued["access_token"]).status_code, 401)
        # The revocation endpoint ends the whole chain.
        revoked = httpx.post(
            host.base + "/revoke", data={"token": issued["refresh_token"], "client_id": client}
        )
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(host.call(issued["access_token"]).status_code, 401)
        # So does the command on the host, from another process.
        second = host.tokens(client)
        self.assertEqual(host.call(second["access_token"]).status_code, 200)
        result = subprocess.run(
            [sys.executable, "-m", "familiekilder", "--root", str(host.root), "remote", "revoke", "--all"],
            capture_output=True, text=True, env=os.environ, check=True,
        )
        self.assertGreaterEqual(json.loads(result.stdout)["revoked_tokens"], 2)
        self.assertEqual(host.call(second["access_token"]).status_code, 401)

    def test_refresh_rotates_and_reuse_ends_the_chain(self) -> None:
        host = Host(self)
        client = fake.register(host.base)["client_id"]
        first = host.tokens(client)
        self.assertIn("offline_access", first["scope"].split())
        renewed = host.token(grant_type="refresh_token", refresh_token=first["refresh_token"], client_id=client)
        self.assertEqual(renewed.status_code, 200, renewed.text)
        second = renewed.json()
        self.assertNotEqual(second["refresh_token"], first["refresh_token"])
        self.assertEqual(host.call(second["access_token"]).status_code, 200)
        replay = host.token(grant_type="refresh_token", refresh_token=first["refresh_token"], client_id=client)
        self.assertEqual(replay.json()["error"], "invalid_grant")
        self.assertEqual(host.call(second["access_token"]).status_code, 401)
        again = host.token(grant_type="refresh_token", refresh_token=second["refresh_token"], client_id=client)
        self.assertEqual(again.json()["error"], "invalid_grant")

    def test_secrets_are_never_stored_in_clear_text(self) -> None:
        host = Host(self)
        storage = fake.MemoryStorage()

        async def flow():
            async with fake.session(host.base, storage) as connected:
                await connected.list_tools()

        asyncio.run(flow())
        secrets_seen = [
            fake.OWNER_CODE,
            fake.OWNER_CODE.replace("-", ""),
            storage.tokens.access_token,
            storage.tokens.refresh_token,
        ]
        for path in host.root.rglob("*"):
            if path.is_file():
                content = path.read_bytes()
                for secret in secrets_seen:
                    self.assertNotIn(secret.encode(), content, path.name)
        stored = json.loads((host.root / ".private/remote/oauth.json").read_text())
        self.assertTrue(stored["access"] and stored["refresh"])
        self.assertEqual(os.stat(host.root / ".private/remote/oauth.json").st_mode & 0o777, 0o600)

    def test_registered_clients_are_listed_without_secrets(self) -> None:
        host = Host(self)
        registered = fake.register(host.base)
        host.tokens(registered["client_id"])
        listed = remote_auth.clients_command(host.root)["clients"]
        self.assertEqual(listed[0]["client_id"], registered["client_id"])
        self.assertEqual(listed[0]["active_connections"], 1)
        self.assertNotIn("client_secret", json.dumps(listed))


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_catalog_is_the_same_over_stdio_and_http(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Config(root, {"spond": frozenset({"messages"}), "vigilo": frozenset()}).save()
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "familiekilder", "--root", directory, "mcp", "serve"],
                env={"PATH": os.defpath, "HOME": directory, "PYTHONPATH": os.environ.get("PYTHONPATH", "")},
            )
            async with asyncio.timeout(30):
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as stdio:
                        stdio_hello = await stdio.initialize()
                        stdio_tools = (await stdio.list_tools()).tools
            port = fake.free_port()
            with patch.dict(os.environ, fake.host_environment(port)):
                config = Config.load(root)
                built = build_http_app(config, host_settings())
                try:
                    with fake.running(built.app, port) as base:
                        async with fake.session(base) as http:
                            http_tools = (await http.list_tools()).tools
                finally:
                    built.runtime.close()
        dump = lambda tools: [tool.model_dump(mode="json") for tool in tools]  # noqa: E731
        self.assertEqual(dump(http_tools), dump(stdio_tools))
        self.assertGreater(len(http_tools), 1)
        self.assertEqual(stdio_hello.server_info.name, "familiekilder-mcp")


class StartupTests(unittest.TestCase):
    def test_http_mode_refuses_to_start_without_owner_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("FAMILIEKILDER_")
            }
            environment["FAMILIEKILDER_PUBLIC_URL"] = "https://oppdiktet.example"
            result = subprocess.run(
                [sys.executable, "-m", "familiekilder", "--root", directory, "mcp", "serve", "--http",
                 "--port", str(fake.free_port())],
                capture_output=True, text=True, env=environment, timeout=30,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("FAMILIEKILDER_OWNER_CODE_HASH", result.stderr)
        with self.assertRaises(HostingError):
            host_settings({"FAMILIEKILDER_PUBLIC_URL": "https://oppdiktet.example"})

    def test_settings_refuse_plain_http_and_weak_hashes(self) -> None:
        base = fake.host_environment(8080)
        with self.assertRaisesRegex(HostingError, "HTTPS"):
            host_settings({**base, "FAMILIEKILDER_PUBLIC_URL": "http://oppdiktet.example"})
        with self.assertRaisesRegex(HostingError, "scrypt"):
            host_settings({**base, "FAMILIEKILDER_OWNER_CODE_HASH": "scrypt:1024:8:1:AAAAAAAAAAAAAAAAAAAAAA:" + "A" * 43})
        self.assertEqual(host_settings(base).resource_url, "http://127.0.0.1:8080/mcp")

    def test_server_output_contains_no_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            port = fake.free_port()
            environment = {**os.environ, **fake.host_environment(port)}
            process = subprocess.Popen(
                [sys.executable, "-m", "familiekilder", "--root", directory, "mcp", "serve", "--http",
                 "--port", str(port)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=environment,
            )
            try:
                base = f"http://127.0.0.1:{port}"
                deadline = time.monotonic() + 30
                while True:
                    try:
                        if httpx.get(base + "/healthz").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if time.monotonic() > deadline:
                        raise AssertionError("server did not start")
                    time.sleep(0.1)
                storage = fake.MemoryStorage()

                async def flow():
                    async with fake.session(base, storage) as connected:
                        await connected.call_tool("family_connections", {})

                asyncio.run(flow())
                wrong = fake.register(base)
                approval = Host.approve.__get__(type("H", (), {"base": base})())
                approval(wrong["client_id"], challenge=_pkce()[1], owner_code="feil-kode-1234")
            finally:
                process.terminate()
                output, _ = process.communicate(timeout=20)
        text = output.decode("utf-8", "replace")
        for secret in (
            fake.OWNER_CODE,
            storage.tokens.access_token,
            storage.tokens.refresh_token,
            storage.client.client_secret or fake.OWNER_CODE,
            "feil-kode-1234",
            fake.INSTALLATION_ID,
        ):
            self.assertNotIn(secret, text)
        self.assertNotIn("code=", text)


def _wait_for_health(base: str, process: subprocess.Popen | None = None) -> None:
    deadline = time.monotonic() + 30
    while True:
        try:
            if httpx.get(base + "/healthz").status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if process is not None and process.poll() is not None:
            raise AssertionError("server exited before it answered")
        if time.monotonic() > deadline:
            raise AssertionError("server did not start")
        time.sleep(0.1)


class StopTests(unittest.TestCase):
    """The host stops an idle server with SIGINT or SIGTERM; that is a normal stop."""

    def _container_command(self, directory: Path) -> tuple[list[str], dict[str, str]]:
        # The container entry point execs "familiekilder-mcp" from PATH. This
        # shim is what the installed console script does, with this interpreter.
        bin_directory = directory / "bin"
        bin_directory.mkdir()
        script = bin_directory / "familiekilder-mcp"
        script.write_text(
            f"#!{sys.executable}\n"
            "from familiekilder.source_cli import main\n"
            "raise SystemExit(main())\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        port = fake.free_port()
        environment = {
            **os.environ,
            **fake.host_environment(port),
            "FAMILIEKILDER_ROOT": str(directory / "data"),
            "PATH": str(bin_directory) + os.pathsep + os.environ.get("PATH", ""),
        }
        command = [
            sys.executable, "-m", "familiekilder.container",
            "mcp", "serve", "--http", "--host", "127.0.0.1", "--port", str(port),
        ]
        return command, environment

    def _stop_container_server(self, signal_number: int) -> tuple[int, float, str]:
        with tempfile.TemporaryDirectory() as name:
            command, environment = self._container_command(Path(name))
            port = environment["FAMILIEKILDER_PUBLIC_URL"].rsplit(":", 1)[1]
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
            )
            try:
                _wait_for_health(f"http://127.0.0.1:{port}", process)
                started = time.monotonic()
                process.send_signal(signal_number)
                _, error = process.communicate(timeout=20)
                elapsed = time.monotonic() - started
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        return process.returncode, elapsed, error.decode("utf-8", "replace")

    def test_container_server_stops_quietly_on_sigint_and_sigterm(self) -> None:
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signal_number.name):
                status, elapsed, error = self._stop_container_server(signal_number)
                self.assertEqual(status, 0, error)
                self.assertLess(elapsed, 5)
                self.assertNotIn("Traceback", error)
                self.assertNotIn("KeyboardInterrupt", error)

    def test_stop_by_signal_closes_the_runtime_and_restores_handlers(self) -> None:
        from familiekilder import remote_http

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        port = fake.free_port()
        environment = patch.dict(os.environ, fake.host_environment(port))
        environment.start()
        self.addCleanup(environment.stop)
        Config(root, {"spond": frozenset({"messages"})}).save()
        config = Config.load(root)
        closed: list[bool] = []
        build = remote_http.build_http_app

        def build_and_watch(*args, **kwargs):
            built = build(*args, **kwargs)
            close = built.runtime.close

            def close_and_record():
                closed.append(True)
                close()

            built.runtime.close = close_and_record
            return built

        for signal_number in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signal_number.name):
                closed.clear()
                before = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}

                def stop_when_up(number=signal_number):
                    _wait_for_health(f"http://127.0.0.1:{port}")
                    os.kill(os.getpid(), number)

                stopper = threading.Thread(target=stop_when_up, daemon=True)
                stopper.start()
                with patch.object(remote_http, "build_http_app", build_and_watch):
                    status = remote_http.serve_http(config, host="127.0.0.1", port=port)
                stopper.join(timeout=5)
                self.assertEqual(status, 0)
                self.assertEqual(closed, [True])
                after = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
                self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
