"""Fakes shared by the tests of familiekilder.

They are not part of the installed package. A test that needs one imports it
from here, never from another test file, so a test run only finds these when
`tests/support` is on the path (scripts/test and pytest put it there).
"""
