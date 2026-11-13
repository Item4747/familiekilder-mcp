from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from familiekilder import __version__
from familiekilder import source_cli

SOURCE_COMMANDS = [
    "password",
    "mcp",
    "connections",
    "configure",
    "auth",
    "onboard",
    "ist-auth",
    "setup",
]


def subcommands(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return {}


def option_strings(parser):
    return {option for action in parser._actions for option in action.option_strings}


class SourceCliParserTests(unittest.TestCase):
    def test_standalone_cannot_enable_write_permissions(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            source_cli.build_parser().parse_args(["configure", "spond", "--write", "messages"])

    def test_version_does_not_load_or_create_a_profile(self) -> None:
        output = io.StringIO()
        with (
            patch.object(source_cli.Config, "load", side_effect=AssertionError("profile loaded")),
            contextlib.redirect_stdout(output),
            self.assertRaises(SystemExit) as exit_status,
        ):
            source_cli.main(["--version"])
        self.assertEqual(exit_status.exception.code, 0)
        self.assertEqual(output.getvalue(), f"familiekilder-mcp {__version__}\n")

    def test_offers_only_source_commands(self) -> None:
        parser = source_cli.build_parser()
        commands = subcommands(parser)
        self.assertEqual(list(commands), SOURCE_COMMANDS)
        self.assertEqual(list(subcommands(commands["mcp"])), ["serve"])
        self.assertNotIn("--agent-intake", option_strings(subcommands(commands["mcp"])["serve"]))
        self.assertIn("--bundle", option_strings(commands["onboard"]))
        self.assertNotIn("--provider-service", option_strings(commands["onboard"]))
        for closed in ("chatgpt", "web", "calendar"):
            with (
                self.subTest(command=closed),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                parser.parse_args([closed])

    def test_connections_on_empty_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "profile"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(source_cli.main(["--root", str(root), "connections"]), 0)
            self.assertTrue(root.is_dir())
        self.assertEqual(json.loads(output.getvalue())["services"], [])

    def test_errors_use_the_program_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = source_cli.main(["--root", directory, "ist-auth", "status"], prog="kilder")
        self.assertEqual(code, 2)
        self.assertTrue(stderr.getvalue().startswith("kilder: "))


class Extension(source_cli.CliExtension):
    def __init__(self) -> None:
        self.calls = []

    def before_command(self, name, subparsers):
        self.calls.append(("before", name))
        if name == "connections":
            extra = subparsers.add_parser("extra", help="Ekstra kommando")
            extra.set_defaults(handler=self.run_extra, create_root=False)

    def extend_command(self, name, parser, groups):
        self.calls.append(("extend", name, sorted(groups)))
        if name == "connections":
            parser.add_argument("--verbose", action="store_true")
            parser.set_defaults(handler=self.run_connections)

    def after_commands(self, subparsers):
        self.calls.append(("after",))
        subparsers.add_parser("last")

    def run_extra(self, args, config):
        return 7

    def run_connections(self, args, config):
        return 3 if args.verbose else 4


class SourceCliExtensionTests(unittest.TestCase):
    def test_extension_adds_commands_flags_and_handlers(self) -> None:
        extension = Extension()
        parser = source_cli.build_parser([extension])
        self.assertEqual(
            list(subcommands(parser)),
            [*SOURCE_COMMANDS[:2], "extra", *SOURCE_COMMANDS[2:], "last"],
        )
        self.assertIn(("extend", "onboard", ["source"]), extension.calls)
        self.assertIn(("extend", "mcp serve", []), extension.calls)
        self.assertEqual(extension.calls[-1], ("after",))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "profile"
            self.assertEqual(
                source_cli.main(["--root", str(root), "extra"], extensions=[extension]), 7
            )
            self.assertFalse(root.exists())
            self.assertEqual(
                source_cli.main(
                    ["--root", str(root), "connections", "--verbose"],
                    extensions=[Extension()],
                ),
                3,
            )
            self.assertTrue(root.is_dir())


if __name__ == "__main__":
    unittest.main()
