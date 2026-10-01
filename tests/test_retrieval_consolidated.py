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

    def test_qualifiers_keep_exact_dated_note_first_and_rank_relevant_section(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            wiki = vault / 'wiki'
            wiki.mkdir()
            (wiki / 'cashflow-manager.md').write_text(
                '# Cashflow Manager\n'
                '## 2026-04-25 — auth and forecast follow-up\n'
                'Auth forecast discussion for the dated review.\n'
                '## 2026-04-25 — billing migration\n'
                'Billing migration discussion for the dated review.\n', encoding='utf-8')
            (wiki / 'cashflow-manager-2026-04-25.md').write_text(
                '# Cashflow Manager 2026-04-25\nAuth forecast session notes.\n', encoding='utf-8')
            (wiki / 'auth-forecast.md').write_text(
                '# Auth Forecast\nCashflow manager auth forecast reference.\n', encoding='utf-8')
            (wiki / 'archive.md').write_text(
                '# Cashflow Manager\n' + 'billing migration ' * 40
                + '\n## 2026-04-25 — auth follow-up\nAuth review only.\n', encoding='utf-8')
            subprocess.run([sys.executable, str(ROOT / 'scripts/bm25-index.py'), 'build', '--vault', str(vault)], check=True, capture_output=True)

            def retrieve(query):
                return subprocess.run([sys.executable, str(ROOT / 'scripts/retrieve.py'), query, '--vault', str(vault)], check=True, capture_output=True, text=True).stdout

            exact = retrieve('cashflow manager 2026-04-25 auth forecast')
            self.assertTrue(exact.startswith('1. wiki/cashflow-manager-2026-04-25.md'), exact)
            self.assertIn('exact_date_match', exact.splitlines()[0])
            date_first = retrieve('2026-04-25 cashflow manager auth forecast')
            self.assertTrue(date_first.startswith('1. wiki/cashflow-manager-2026-04-25.md'), date_first)
            self.assertIn('exact_date_match', date_first.splitlines()[0])
            date_first_without_qualifiers = retrieve('2026-04-25 cashflow manager')
            self.assertTrue(date_first_without_qualifiers.startswith('1. wiki/cashflow-manager-2026-04-25.md'), date_first_without_qualifiers)

            (wiki / 'cashflow-manager-2026-04-25.md').unlink()
            subprocess.run([sys.executable, str(ROOT / 'scripts/bm25-index.py'), 'build', '--vault', str(vault)], check=True, capture_output=True)
            section = retrieve('cashflow manager 2026-04-25 billing migration')
            self.assertTrue(section.startswith('1. wiki/cashflow-manager.md'), section)
            self.assertIn('exact_date_match', section.splitlines()[0])
            date_first_section = retrieve('2026-04-25 cashflow manager billing migration')
            self.assertTrue(date_first_section.startswith('1. wiki/cashflow-manager.md'), date_first_section)
            self.assertIn('exact_date_match', date_first_section.splitlines()[0])

    def test_standalone_dated_filename_matches_without_canonical_title(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            wiki = vault / 'wiki'
            wiki.mkdir()
            (wiki / 'cashflow-manager-2026-04-25.md').write_text(
                '# Cashflow Manager 2026-04-25\nAuth forecast session notes.\n', encoding='utf-8')
            (wiki / 'billing-manager-2026-04-25.md').write_text(
                '# Billing Manager 2026-04-25\n' + 'cashflow manager auth forecast billing ' * 50,
                encoding='utf-8')
            (wiki / 'manager-2026-04-25.md').write_text(
                '# Manager 2026-04-25\n' + 'cashflow manager auth forecast ' * 50,
                encoding='utf-8')
            subprocess.run([sys.executable, str(ROOT / 'scripts/bm25-index.py'), 'build', '--vault', str(vault)], check=True, capture_output=True)

            def retrieve(query):
                return subprocess.run([sys.executable, str(ROOT / 'scripts/retrieve.py'), query, '--vault', str(vault)], check=True, capture_output=True, text=True).stdout

            for query in (
                'cashflow manager 2026-04-25',
                '2026-04-25 cashflow manager',
                'cashflow manager 2026-04-25 auth forecast billing',
                '2026-04-25 cashflow manager auth forecast billing',
            ):
                with self.subTest(query=query):
                    output = retrieve(query)
                    self.assertTrue(output.startswith('1. wiki/cashflow-manager-2026-04-25.md'), output)
                    self.assertIn('exact_date_match', output.splitlines()[0])

    def test_qualifiers_do_not_promote_partial_dated_filename_topics(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            wiki = vault / 'wiki'
            wiki.mkdir()
            (wiki / 'cashflow-manager.md').write_text(
                '# Cashflow Manager\n## 2026-04-25 — billing migration\nBilling migration details.\n',
                encoding='utf-8')
            (wiki / 'billing-2026-04-25.md').write_text(
                '# Billing\nBilling migration details.\n', encoding='utf-8')
            (wiki / 'manager-2026-04-25.md').write_text(
                '# Manager\nManager billing migration details.\n', encoding='utf-8')
            subprocess.run([sys.executable, str(ROOT / 'scripts/bm25-index.py'), 'build', '--vault', str(vault)], check=True, capture_output=True)
            result = subprocess.run([
                sys.executable, str(ROOT / 'scripts/retrieve.py'),
                'cashflow manager 2026-04-25 billing migration', '--vault', str(vault)],
                check=True, capture_output=True, text=True).stdout
            self.assertTrue(result.startswith('1. wiki/cashflow-manager.md'), result)

    def test_mixed_case_markdown_suffix_is_indexed_and_retrievable(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            wiki = vault / 'wiki'
            wiki.mkdir()
            (wiki / 'Upper.MD').write_text('# Case Needle\nDistinctive retrieval token.\n', encoding='utf-8')
            subprocess.run([sys.executable, str(ROOT / 'scripts/bm25-index.py'), 'build', '--vault', str(vault)], check=True, capture_output=True)
            result = subprocess.run([
                sys.executable, str(ROOT / 'scripts/retrieve.py'), 'distinctive', '--vault', str(vault)],
                check=True, capture_output=True, text=True).stdout
            self.assertTrue(result.startswith('1. wiki/Upper.MD'), result)

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
