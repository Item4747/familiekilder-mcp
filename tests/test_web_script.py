"""REGISTRY has what a web page needs per service, and web_script() delivers it as JavaScript."""
import json
import shutil
import subprocess
import unittest

from familiekilder.sources import REGISTRY, web_script


class WebScriptTests(unittest.TestCase):
    def test_every_service_has_what_the_pages_show(self):
        for source in REGISTRY.values():
            with self.subTest(source=source.name):
                self.assertTrue(source.label and source.letter and source.description)
                self.assertIn(source.kind, {'school', 'sport', 'mail', 'other'})

    @unittest.skipUnless(shutil.which('node'), 'Node is required to run the generated script')
    def test_generated_script_matches_the_registry(self):
        result = subprocess.run(
            ['node', '-e', "const vm=require('node:vm'),c=vm.createContext({});"
             "vm.runInContext(process.argv[1],c);process.stdout.write(JSON.stringify(c.Services));",
             web_script()],
            capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {
            'labels': {s.name: s.label for s in REGISTRY.values()},
            'letters': {s.name: s.letter for s in REGISTRY.values()},
            'descriptions': {s.name: s.description for s in REGISTRY.values()},
            'kinds': {s.name: s.kind for s in REGISTRY.values()},
            'readOnly': ['flyt', 'gmail'],
        })


if __name__ == '__main__':
    unittest.main()
