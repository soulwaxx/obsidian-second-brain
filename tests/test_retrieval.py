import contextlib
import importlib.util
import io
import json
import os
import shutil
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class Ollama(BaseHTTPRequestHandler):
    status = 200
    redirect = None
    seen = 0
    inputs = []

    def do_POST(self):
        type(self).seen += 1
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        type(self).inputs = body.get("input", [])
        if type(self).redirect:
            self.send_response(302)
            self.send_header("Location", type(self).redirect)
            self.end_headers()
            return
        vectors = [[1, 0]] + ([[0, 1], [0.8, 0.6]] if len(type(self).inputs) == 3 else [])
        self.send_response(type(self).status)
        self.end_headers()
        self.wfile.write(json.dumps({"embeddings": vectors}).encode())

    def log_message(self, *_):
        pass


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name)
        (self.vault / "wiki").mkdir()
        (self.vault / "wiki" / "coffee.md").write_text("coffee espresso beans roast aroma", encoding="utf-8")
        (self.vault / "wiki" / "planets.md").write_text("mars orbit planet telescope astronomy", encoding="utf-8")
        self.index = self.vault / ".vault-meta/retrieval/bm25.json"
        self.build()

    def tearDown(self):
        self.temp.cleanup()

    def build(self):
        subprocess.run([sys.executable, str(ROOT / "scripts/bm25-index.py"), "build", "--vault", str(self.vault)], check=True, capture_output=True)

    def retrieve(self, query="espresso beans", *extra, env=None):
        return subprocess.run([sys.executable, str(ROOT / "scripts/retrieve.py"), query, "--vault", str(self.vault), *extra], text=True, capture_output=True, env=env)

    def test_index_output_cannot_overwrite_user_pages(self):
        note = self.vault / "wiki/quickstart.md"
        note.write_text("sentinel user note\n", encoding="utf-8")
        result = subprocess.run([sys.executable, str(ROOT / "scripts/bm25-index.py"), "build", "--vault", str(self.vault), "--index", "wiki/quickstart.md"], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("inside .vault-meta/retrieval", result.stderr)
        self.assertEqual(note.read_text(encoding="utf-8"), "sentinel user note\n")
        result = subprocess.run([sys.executable, str(ROOT / "scripts/bm25-index.py"), "build", "--vault", str(self.vault)], check=True, text=True, capture_output=True)
        self.assertIn("Indexed", result.stdout)

    def test_ranking_and_rebuild_update(self):
        result = self.retrieve()
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.startswith("1. wiki/coffee.md"), result.stdout)
        (self.vault / "wiki" / "coffee.md").unlink()
        self.build()
        result = self.retrieve("espresso")
        self.assertIn("No indexed matches", result.stdout)
        (self.vault / "wiki" / "new.md").write_text("espresso beans", encoding="utf-8")
        subprocess.run([sys.executable, str(ROOT / "scripts/bm25-index.py"), "update", "--vault", str(self.vault)], check=True, capture_output=True)
        self.assertTrue(self.retrieve().stdout.startswith("1. wiki/new.md"))

    def test_missing_and_corrupt_index_fall_back(self):
        self.index.unlink()
        result = self.retrieve()
        self.assertEqual(result.returncode, 0)
        self.assertIn("wiki/index.md navigation", result.stderr)
        self.index.parent.mkdir(parents=True, exist_ok=True)
        self.index.write_text("{bad", encoding="utf-8")
        result = self.retrieve()
        self.assertEqual(result.returncode, 0)
        self.assertIn("wiki/index.md navigation", result.stderr)
        huge = {"version": 1, "documents": 1, "lengths": {"wiki/coffee.md": 10**200},
                "terms": {"espresso": {"wiki/coffee.md": 1}}}
        self.index.write_text(json.dumps(huge), encoding="utf-8")
        result = self.retrieve()
        self.assertEqual(result.returncode, 0)
        self.assertIn("wiki/index.md navigation", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_index_excludes_reserved_and_hidden_pages(self):
        excluded = ["index.md", "log.md", "_plan.md", ".raw/private.md", "topic/.hidden/page.md"]
        for relative in excluded:
            path = self.vault / "wiki" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("confidential secretword", encoding="utf-8")
        self.build()
        data = json.loads(self.index.read_text(encoding="utf-8"))
        self.assertEqual(set(data["lengths"]), {"wiki/coffee.md", "wiki/planets.md"})
        result = self.retrieve("secretword")
        self.assertIn("No indexed matches", result.stdout)

        outside = self.vault / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("outsideword", encoding="utf-8")
        (self.vault / "wiki" / "escape").symlink_to(outside, target_is_directory=True)
        self.build()
        data = json.loads(self.index.read_text(encoding="utf-8"))
        self.assertFalse(any("secret.md" in page for page in data["lengths"]))

    def test_forged_index_paths_never_print(self):
        forged = {"version": 1, "documents": 1, "lengths": {"wiki/../.env": 1},
                  "terms": {"leak": {"wiki/../.env": 1}}}
        self.index.write_text(json.dumps(forged), encoding="utf-8")
        result = self.retrieve("leak")
        self.assertEqual(result.returncode, 0)
        self.assertNotIn(".env", result.stdout)
        self.assertIn("wiki/index.md navigation", result.stdout)

    def test_index_symlinked_cache_and_temp_are_refused(self):
        outside = self.vault / "outside"
        outside.mkdir()
        sentinel = outside / "bm25.json"
        sentinel.write_text("unchanged", encoding="utf-8")
        cache_dir = self.vault / ".vault-meta/retrieval"
        shutil.rmtree(cache_dir)
        cache_dir.parent.mkdir(parents=True, exist_ok=True)
        cache_dir.symlink_to(outside, target_is_directory=True)
        result = subprocess.run([sys.executable, str(ROOT / "scripts/bm25-index.py"), "build", "--vault", str(self.vault)], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
        cache_dir.unlink()
        cache_dir.mkdir()
        temp_link = cache_dir / "bm25.json.tmp"
        temp_link.symlink_to(sentinel)
        result = subprocess.run([sys.executable, str(ROOT / "scripts/bm25-index.py"), "build", "--vault", str(self.vault)], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
        self.assertTrue(temp_link.is_symlink())
        temp_link.unlink()
        cache_link = cache_dir / "bm25.json"
        cache_link.symlink_to(sentinel)
        result = subprocess.run([sys.executable, str(ROOT / "scripts/bm25-index.py"), "build", "--vault", str(self.vault)], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")

    def test_ollama_embedding_rerank_local_and_failure_fallback(self):
        Ollama.seen = 0
        Ollama.redirect = None
        Ollama.status = 200
        server = HTTPServer(("127.0.0.1", 0), Ollama)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            ordinary = self.retrieve()
            self.assertEqual(Ollama.seen, 0)
            proxy_env = os.environ.copy()
            proxy_env.update({"HTTP_PROXY": "http://127.0.0.1:1", "http_proxy": "http://127.0.0.1:1", "NO_PROXY": "", "no_proxy": ""})
            ranked = self.retrieve("coffee mars", "--rerank", "--ollama-url", base, env=proxy_env)
            self.assertEqual(Ollama.seen, 1)
            self.assertTrue(ranked.stdout.startswith("1. wiki/planets.md"), ranked.stdout)
            self.assertEqual(Ollama.inputs[0], "coffee mars")
            self.assertIn("coffee espresso beans roast aroma", Ollama.inputs)
            self.assertIn("mars orbit planet telescope astronomy", Ollama.inputs)
            self.assertTrue(all(len(text) <= 8000 for text in Ollama.inputs[1:]))
            Ollama.status = 503
            failed = self.retrieve("coffee mars", "--rerank", "--ollama-url", base)
            self.assertIn("wiki/coffee.md", failed.stdout)
            self.assertIn("using BM25", failed.stderr)
            Ollama.status = 200
            remote = self.retrieve("coffee mars", "--rerank", "--ollama-url", "http://example.com")
            self.assertIn("using BM25", remote.stderr)
            self.assertEqual(Ollama.seen, 2)

            # A redirect to a different server is rejected, never followed.
            redirect_target = HTTPServer(("127.0.0.1", 0), Ollama)
            redirect_thread = threading.Thread(target=redirect_target.serve_forever, daemon=True)
            redirect_thread.start()
            try:
                Ollama.redirect = f"http://127.0.0.1:{redirect_target.server_port}/api/embed"
                before = Ollama.seen
                redirected = self.retrieve("coffee mars", "--rerank", "--ollama-url", base)
                self.assertIn("using BM25", redirected.stderr)
                self.assertEqual(Ollama.seen, before + 1)
            finally:
                Ollama.redirect = None
                redirect_target.shutdown()
                redirect_thread.join()
                redirect_target.server_close()
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_provision_rollback_preserves_existing_exclude(self):
        git_info = self.vault / ".git/info"
        git_info.mkdir(parents=True)
        exclude = git_info / "exclude"
        original = "# user rules\n*.private\n"
        exclude.write_text(original, encoding="utf-8")
        preview = subprocess.run([sys.executable, str(ROOT / "scripts/provision-retrieval.py"), "--vault", str(self.vault)], check=True, text=True, capture_output=True)
        plan = json.loads(preview.stdout)

        spec = importlib.util.spec_from_file_location("provision_retrieval", ROOT / "scripts/provision-retrieval.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        real_open = Path.open

        def fail_exclude_temp(path, *args, **kwargs):
            if path.name == "exclude.provision.tmp":
                raise OSError("injected failure")
            return real_open(path, *args, **kwargs)

        argv = ["provision-retrieval.py", "--vault", str(self.vault), "--apply", "--confirm", plan["planHash"]]
        with patch.object(sys, "argv", argv), patch.object(Path, "open", fail_exclude_temp), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                module.main()
        self.assertEqual(exclude.read_text(encoding="utf-8"), original)
        self.assertFalse((self.vault / "scripts/retrieve.py").exists())
        self.assertFalse((self.vault / "scripts/bm25-index.py").exists())
        self.assertFalse((self.vault / "scripts/contextual-prefix.py").exists())

    def test_provision_handles_linked_worktree_ignore_explicitly(self):
        # A plausible but synthetic .git pointer must fail before writes.
        git_file = self.vault / ".git"
        git_file.write_text("gitdir: ../common/worktrees/vault\n", encoding="utf-8")
        result = subprocess.run([sys.executable, str(ROOT / "scripts/provision-retrieval.py"), "--vault", str(self.vault)], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a Git worktree rooted at this vault", result.stderr)
        self.assertFalse((self.vault / "scripts/retrieve.py").exists())
        git_file.unlink()

        # Exercise actual linked-worktree metadata and Git's effective ignore rules.
        repo = self.vault / "source-repo"
        linked = self.vault / "linked-vault"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
        (repo / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(linked), "HEAD"], check=True)
        self.assertTrue((linked / ".git").is_file())

        provision = ROOT / "scripts/provision-retrieval.py"
        gitignore = linked / ".gitignore"
        gitignore.write_text(".vault-meta/retrieval/\n!.vault-meta/retrieval/\n", encoding="utf-8")
        result = subprocess.run([sys.executable, str(provision), "--vault", str(linked)], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not effectively ignored", result.stderr)
        self.assertFalse((linked / "scripts/retrieve.py").exists())

        gitignore.write_text(".vault-meta/retrieval/bm25.json\n", encoding="utf-8")
        result = subprocess.run([sys.executable, str(provision), "--vault", str(linked)], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("directory and BM25 file must both be ignored", result.stderr)
        self.assertFalse((linked / "scripts/retrieve.py").exists())

        gitignore.write_text("!.vault-meta/retrieval/\n.vault-meta/retrieval/\n", encoding="utf-8")
        preview = subprocess.run([sys.executable, str(provision), "--vault", str(linked)], check=True, text=True, capture_output=True)
        plan = json.loads(preview.stdout)
        self.assertIn("effective Git ignore check excludes", plan["cacheIgnore"])
        self.assertEqual(len(plan["writes"]), 3)
        apply = subprocess.run([sys.executable, str(provision), "--vault", str(linked), "--apply", "--confirm", plan["planHash"]], check=True, text=True, capture_output=True)
        self.assertIn("Provisioned", apply.stdout)
        self.assertEqual(gitignore.read_text(encoding="utf-8"), "!.vault-meta/retrieval/\n.vault-meta/retrieval/\n")

    def test_provision_refuses_symlinked_paths(self):
        outside = self.vault / "outside"
        outside.mkdir()
        (self.vault / "scripts").symlink_to(outside, target_is_directory=True)
        result = subprocess.run([sys.executable, str(ROOT / "scripts/provision-retrieval.py"), "--vault", str(self.vault)], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(outside.iterdir()), [])
        (self.vault / "scripts").unlink()

        (self.vault / ".git/info").mkdir(parents=True)
        target = outside / "exclude"
        target.write_text("keep\n", encoding="utf-8")
        (self.vault / ".git/info/exclude").symlink_to(target)
        result = subprocess.run([sys.executable, str(ROOT / "scripts/provision-retrieval.py"), "--vault", str(self.vault)], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(encoding="utf-8"), "keep\n")
        self.assertFalse((self.vault / "scripts/retrieve.py").exists())

        (self.vault / ".git/info/exclude").unlink()
        (self.vault / ".git/info").rmdir()
        (self.vault / ".git").rmdir()
        external_git = outside / "git-dir"
        (external_git / "info").mkdir(parents=True)
        external_exclude = external_git / "info/exclude"
        external_exclude.write_text("external\n", encoding="utf-8")
        (self.vault / ".git").symlink_to(external_git, target_is_directory=True)
        result = subprocess.run([sys.executable, str(ROOT / "scripts/provision-retrieval.py"), "--vault", str(self.vault)], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(external_exclude.read_text(encoding="utf-8"), "external\n")
        self.assertFalse((self.vault / "scripts/retrieve.py").exists())

    def test_provision_refuses_overwrite_and_adds_local_exclude(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/provision-retrieval.py"), "--vault", str(self.vault)], check=True, text=True, capture_output=True)
        plan = json.loads(result.stdout)
        self.assertEqual(len(plan["writes"]), 3)
        apply = subprocess.run([sys.executable, str(ROOT / "scripts/provision-retrieval.py"), "--vault", str(self.vault), "--apply", "--confirm", plan["planHash"]], check=True, text=True, capture_output=True)
        self.assertIn("Provisioned", apply.stdout)
        conflict = subprocess.run([sys.executable, str(ROOT / "scripts/provision-retrieval.py"), "--vault", str(self.vault)], text=True, capture_output=True)
        self.assertNotEqual(conflict.returncode, 0)


if __name__ == "__main__":
    unittest.main()
