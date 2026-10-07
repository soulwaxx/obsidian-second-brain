"""Supported path loading, registration, and warning-free package imports."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from module_loading import load_source


class ModuleLoadingTests(unittest.TestCase):
    def test_registration_metadata_and_repeat_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "hyphen-module.py"
            path.write_text("import sys\nregistered = sys.modules[__name__]\nruns = globals().get('runs', 0) + 1\n")
            name = "module_loading_fixture"
            self.addCleanup(sys.modules.pop, name, None)
            module = load_source(name, path)
            self.assertIs(module.registered, module)
            self.assertEqual(module.__file__, str(path))
            self.assertEqual(module.__spec__.origin, str(path))
            self.assertEqual(module.__package__, "")
            self.assertIs(load_source(name, path), module)
            self.assertEqual(module.runs, 2)

    def test_failure_removes_partial_registration(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "broken.py"
            path.write_text("raise RuntimeError('fixture failure')\n")
            with self.assertRaisesRegex(RuntimeError, "fixture failure"):
                load_source("module_loading_broken", path)
            self.assertNotIn("module_loading_broken", sys.modules)

    def test_package_metadata_supports_relative_imports(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "fixture"
            package.mkdir()
            (package / "__init__.py").write_text("from .child import value\n")
            (package / "child.py").write_text("value = 42\n")
            self.addCleanup(sys.modules.pop, "module_loading_package.child", None)
            self.addCleanup(sys.modules.pop, "module_loading_package", None)
            module = load_source("module_loading_package", package / "__init__.py")
            self.assertEqual(module.value, 42)
            self.assertEqual(module.__package__, "module_loading_package")

    def test_legacy_provisioned_helpers_run_without_checkout_imports(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary).resolve()
            (vault / "wiki").mkdir()
            (vault / "wiki/topic.md").write_text("# Topic\n\nlexical fixture evidence\n")
            provision = [sys.executable, str(ROOT / "scripts/provision-retrieval.py"), "--vault", str(vault)]
            preview = subprocess.run(provision, check=True, capture_output=True, text=True)
            import json
            plan = json.loads(preview.stdout)
            subprocess.run([*provision, "--apply", "--confirm", plan["planHash"]],
                           check=True, capture_output=True)
            env = {**os.environ, "PYTHONPATH": "", "PYTHONWARNINGS": "error::DeprecationWarning"}
            subprocess.run([sys.executable, str(vault / "scripts/bm25-index.py"), "build", "--vault", str(vault)],
                           cwd=temporary, env=env, check=True, capture_output=True)
            result = subprocess.run([sys.executable, str(vault / "scripts/retrieve.py"), "lexical", "--vault", str(vault)],
                                    cwd=temporary, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("wiki/topic.md", result.stdout)

    def test_runtime_modules_import_with_deprecations_as_errors(self):
        paths = ["obsidian-second-brain.py", "retrieve.py", "semantic-chunks.py",
                 "benchmark-semantic-retrieval.py", "wiki_batch.py", "local_capture.py",
                 "evidence_reports.py"]
        code = "from module_loading import load_source\n" + "\n".join(
            f"load_source('warning_fixture_{i}', {str(ROOT / 'scripts' / path)!r})"
            for i, path in enumerate(paths))
        with tempfile.TemporaryDirectory() as temporary:
            env = {**os.environ, "PYTHONPATH": str(ROOT / "scripts"),
                   "OBSIDIAN_AGENT_CONFIG": str(Path(temporary) / "absent.json"),
                   "OBSIDIAN_VAULT_PATH": ""}
            result = subprocess.run([sys.executable, "-W", "error::DeprecationWarning", "-c", code],
                                    cwd=temporary, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
