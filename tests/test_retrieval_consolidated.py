"""Retrieval regressions for dated sections in canonical wiki pages."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]


class AdversarialOllama(BaseHTTPRequestHandler):
    requests = 0

    def do_POST(self):
        type(self).requests += 1
        inputs = json.loads(self.rfile.read(int(self.headers["Content-Length"])))['input']
        vectors = [[1, 0]] + [[0, 1] if '# Cashflow Manager\n' in text else [1, 0] for text in inputs[1:]]
        body = json.dumps({'embeddings': vectors}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class ConsolidatedRetrievalTests(unittest.TestCase):
    def test_canonical_page_wins_topic_and_dated_query_with_rerank(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            (vault / 'wiki/sources').mkdir(parents=True)
            (vault / 'wiki/cashflow-manager.md').write_text(
                '# Cashflow Manager\n' + 'Architecture, finance, and transactions.\n' * 100
                + '## 2026-04-25 — auth and forecast follow-up\nCashflow Manager review.\n', encoding='utf-8')
            (vault / 'wiki/sources/cashflow-manager-repo.md').write_text(
                '# Cashflow Manager Repo\n' + 'Cashflow Manager 2026-04-25 status.\n' * 3,
                encoding='utf-8')
            subprocess.run([sys.executable, str(ROOT / 'scripts/bm25-index.py'), 'build', '--vault', str(vault)], check=True, capture_output=True)

            def retrieve(query, *options):
                result = subprocess.run([sys.executable, str(ROOT / 'scripts/retrieve.py'), query, '--vault', str(vault), *options], capture_output=True, text=True, check=True)
                return result.stdout

            self.assertTrue(retrieve('cashflow manager').startswith('1. wiki/cashflow-manager.md'))
            dated = 'cashflow manager 2026-04-25'
            self.assertTrue(retrieve(dated).startswith('1. wiki/cashflow-manager.md'))
            server = HTTPServer(('127.0.0.1', 0), AdversarialOllama)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            AdversarialOllama.requests = 0
            try:
                ranked = retrieve(dated, '--rerank', '--ollama-url', f'http://127.0.0.1:{server.server_port}')
                self.assertTrue(ranked.startswith('1. wiki/cashflow-manager.md'), ranked)
                self.assertEqual(AdversarialOllama.requests, 0)
                generic = retrieve('cashflow manager', '--rerank', '--ollama-url', f'http://127.0.0.1:{server.server_port}')
                self.assertTrue(generic.startswith('1. wiki/sources/cashflow-manager-repo.md'), generic)
                self.assertIn('\tcosine=', generic)
                self.assertEqual(AdversarialOllama.requests, 1)
            finally:
                server.shutdown()
                thread.join()
                server.server_close()

    def test_exact_dated_filename_takes_precedence_over_canonical_section(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            wiki = vault / 'wiki'
            wiki.mkdir()
            (wiki / 'cashflow-manager.md').write_text(
                '# Cashflow Manager\n## 2026-04-25 — follow-up\n' + 'cashflow manager\n' * 40,
                encoding='utf-8')
            (wiki / 'cashflow-manager-2026-04-25.md').write_text(
                '# Session 2026-04-25\nCashflow Manager follow-up.\n', encoding='utf-8')
            subprocess.run([sys.executable, str(ROOT / 'scripts/bm25-index.py'), 'build', '--vault', str(vault)], check=True, capture_output=True)
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/retrieve.py'), 'cashflow manager 2026-04-25', '--vault', str(vault)], check=True, capture_output=True, text=True)
            self.assertTrue(result.stdout.startswith('1. wiki/cashflow-manager-2026-04-25.md'), result.stdout)


if __name__ == '__main__':
    unittest.main()
