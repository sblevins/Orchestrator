"""Provider-scoped worker families and capability-based effort without API calls."""

import shutil
import subprocess
import unittest
from pathlib import Path

from orchestrator.models import model_family, normalize_model, selector_family

ROOT = Path(__file__).resolve().parents[1]


class PiWorkerFamilyTests(unittest.TestCase):
    def test_pi_names_do_not_route_to_anthropic(self):
        for name in ("Astra", "SOL"):
            self.assertIsNone(model_family(name))
            self.assertEqual(selector_family(name), name.lower())
            self.assertEqual(normalize_model(name), name.lower())
        self.assertEqual(normalize_model("gpt-6.1-sol"), "gpt-6.1-sol")

    @unittest.skipUnless(shutil.which("node"), "Node is required")
    def test_exact_first_numeric_catalog_and_supported_effort(self):
        script = r"""
import assert from 'node:assert/strict';
import {resolveWorkerModel, resolveWorkerEffort} from './orchestrator/pi_models.mjs';
let models = [];
const runtime = {
  getModel: (provider, id) => models.find(m => m.provider === provider && m.id === id),
  getModels: provider => models.filter(m => m.provider === provider),
};
const model = (id, provider = 'openai-codex') => ({id, provider});
const select = (name = 'Sol', provider = 'openai-codex') => resolveWorkerModel(runtime, provider, name);
models = ['gpt-6.9-sol', 'gpt-6.10-sol', 'gpt-99-sol-preview', 'gpt-100-sol-fast',
  'gpt-101-sol-pro', 'gpt-102-sol-latest', 'gpt-103-sol-20260230'].map(id => model(id));
models.push(model('gpt-999-sol', 'other-provider'));
assert.equal(select().id, 'gpt-6.10-sol');
assert.equal(select('gpt-6.9-sol').id, 'gpt-6.9-sol');
assert.equal(select('gpt-99-sol-preview').id, 'gpt-99-sol-preview');
assert.throws(() => select('Sol', 'unknown-provider'), /No stable base model/);
models.push(model('Sol'));
assert.equal(select().id, 'Sol');
models = ['gpt-6-astra', 'gpt-6.0-astra'].map(id => model(id));
assert.throws(() => select('ASTRA'), /Ambiguous/);
models = ['gpt-6-astra-20260228', 'gpt-6-astra-20260301', 'gpt-6-astra'].map(id => model(id));
assert.equal(select('Astra').id, 'gpt-6-astra-20260301');
models = [model('openai/gpt-6.1-sol', 'openrouter')];
assert.equal(select('sol', 'openrouter').id, 'openai/gpt-6.1-sol');
assert.throws(() => select('sol '), /unavailable/);
const maximum = ['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'];
assert.deepEqual(resolveWorkerEffort({}, 'max-supported', () => maximum), {resolved:'max', supported:maximum});
assert.equal(resolveWorkerEffort({}, 'max-supported', () => maximum.slice(0, -1)).resolved, 'xhigh');
assert.equal(resolveWorkerEffort({}, 'xhigh', () => maximum).resolved, 'xhigh');
assert.throws(() => resolveWorkerEffort({}, 'max', () => maximum.slice(0, -1)), /unsupported/);
assert.throws(() => resolveWorkerEffort({}, 'max-supported', () => []), /unsupported/);
console.log('worker family checks passed');
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("worker family checks passed", result.stdout)
