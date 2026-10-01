"""Exercise the installed Pi TypeScript loader without inference or network access."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator.frontends import ROOT

HARNESS = r"""
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
const require = createRequire(pathToFileURL(process.argv[3]));
const {createJiti} = require('jiti');
const jiti = createJiti(import.meta.url, {fsCache: false});
const {resolveForegroundModel, isAnthropicFamily} = await jiti.import(process.argv[2]);
const makeModel = (id, provider = 'anthropic') => ({id, provider});
let models = [];
const registry = {
  find: (provider, id) => models.find(model => model.provider === provider && model.id === id),
  getAll: () => models,
};
const select = (requested = 'opus', provider = 'anthropic') => resolveForegroundModel(registry, provider, requested);
models = ['claude-opus-4-9', 'claude-opus-4-10'].map(id => makeModel(id));
assert.equal(select('OpUs').id, 'claude-opus-4-10');
assert.equal(select('claude-opus-4-9').id, 'claude-opus-4-9');
models.push(makeModel('claude-opus-99-preview'), makeModel('claude-opus-99[1m]'),
  makeModel('claude-opus-99-latest'), makeModel('claude-opus-99-20250230'),
  makeModel('claude-opus-99', 'openrouter'));
assert.equal(select().id, 'claude-opus-4-10');
assert.equal(select('claude-opus-99-preview').id, 'claude-opus-99-preview');
assert.throws(() => select(' opus'), /not found/);
assert.throws(() => select('opus '), /not found/);
assert.throws(() => select('opus', 'openrouter'), /not found/);
assert.throws(() => select('unknown'), /not found/);
assert.equal(isAnthropicFamily('FABLE'), true);
assert.equal(isAnthropicFamily('fable '), false);
for (const family of ['opus', 'sonnet', 'haiku', 'fable']) {
  models = [makeModel(`claude-3-7-${family}-20250219`), makeModel(`claude-${family}-4`)];
  assert.equal(select(family).id, `claude-${family}-4`);
}
models = ['claude-3-7-opus-20250219', 'claude-opus-3-7-20250220',
  'claude-opus-3-7', 'claude-opus-3-7-20250229'].map(id => makeModel(id));
assert.equal(select().id, 'claude-opus-3-7-20250220');
models = ['claude-opus-4', 'claude-opus-4-0'].map(id => makeModel(id));
assert.throws(() => select(), /ambiguous.*exact model ID/i);
models = ['claude-4-5-opus-20240229', 'claude-opus-4-5-20240229'].map(id => makeModel(id));
assert.throws(() => select(), /ambiguous.*exact model ID/i);
models.reverse();
assert.throws(() => select(), /ambiguous.*exact model ID/i);
models = [makeModel('opus'), makeModel('claude-opus-99')];
assert.equal(select().id, 'opus'); // Exact custom catalog IDs always win.
models = [makeModel(' odd CUSTOM id ')];
assert.equal(select(' odd CUSTOM id ').id, ' odd CUSTOM id ');
models = [makeModel('claude-opus-5-preview')];
assert.throws(() => select(), /not found/);
console.log('family resolution passed');
"""


@unittest.skipUnless(shutil.which("pi") and shutil.which("node"), "Installed Pi is required")
class PiModelFamilyTests(unittest.TestCase):
    def test_catalog_family_resolution(self):
        with tempfile.TemporaryDirectory() as temporary:
            harness = Path(temporary) / "families.mjs"
            harness.write_text(HARNESS)
            environment = dict(os.environ, PI_OFFLINE="1")
            result = subprocess.run(
                [
                    "node",
                    str(harness),
                    str(ROOT / ".pi/lib/model-families.ts"),
                    str(Path(shutil.which("pi")).resolve()),
                ],
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("family resolution passed", result.stdout)
