#!/usr/bin/env python3
"""Rank local wiki Markdown with BM25; optionally rerank with local Ollama embeddings."""
import argparse
import ipaddress
import json
import math
import re
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

from importlib.machinery import SourceFileLoader

_index_module = SourceFileLoader("bm25_index", str(Path(__file__).with_name("bm25-index.py"))).load_module()
DEFAULT_INDEX = Path(".vault-meta/retrieval/bm25.json")
MAX_DOCUMENT_CHARS = 8000
MAX_QUERY_CHARS = 4000
DATE = re.compile(r"(?<!\d)\d{4}-\d{2}(?:-\d{2})?(?!\d)")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_endpoint(value):
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "http" or not parsed.hostname:
        raise ValueError("Ollama URL must use http:// on localhost or a loopback IP")
    host = parsed.hostname.casefold()
    try:
        local = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = host == "localhost"
    if not local or parsed.username or parsed.password:
        raise ValueError("Ollama endpoint must be localhost or a loopback IP without credentials")
    return value.rstrip("/") + "/api/embed"


def embeddings(inputs, endpoint, model, timeout):
    body = json.dumps({"model": model, "input": inputs}).encode()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    request = urllib.request.Request(endpoint, data=body, headers={"Content-Type": "application/json"})
    with opener.open(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    vectors = data["embeddings"]
    if not isinstance(vectors, list) or len(vectors) != len(inputs):
        raise ValueError("Ollama returned an invalid embedding count")
    checked = []
    dimension = None
    for vector in vectors:
        if not isinstance(vector, list) or not vector:
            raise ValueError("Ollama returned an invalid embedding vector")
        if dimension is None:
            dimension = len(vector)
        if len(vector) != dimension or any(not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x) for x in vector):
            raise ValueError("Ollama returned malformed embedding values")
        checked.append([float(x) for x in vector])
    return checked


def rerank(query, results, vault, endpoint, model, timeout):
    url = local_endpoint(endpoint)
    inputs = [query[:MAX_QUERY_CHARS]]
    for page, _ in results:
        candidate = Path(page)
        if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts or candidate.parts[0] != "wiki":
            raise ValueError("index contains an unsafe candidate path")
        path = vault / candidate
        if not _index_module.eligible(path, vault):
            raise ValueError("index contains a reserved, hidden, or unsafe candidate")
        inputs.append(path.read_text(encoding="utf-8", errors="replace")[:MAX_DOCUMENT_CHARS])
    vectors = embeddings(inputs, url, model, timeout)
    q = vectors[0]
    qnorm = math.sqrt(sum(x * x for x in q))
    if not qnorm:
        raise ValueError("Ollama returned a zero query embedding")
    scored = []
    for idx, result in enumerate(results, 1):
        vector = vectors[idx]
        norm = math.sqrt(sum(x * x for x in vector))
        similarity = sum(a * b for a, b in zip(q, vector)) / (qnorm * norm) if norm else -1.0
        scored.append((result[0], result[1], similarity, idx))
    scored.sort(key=lambda item: (-item[2], item[3]))
    return [(page, similarity) for page, _, similarity, _ in scored]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", help="natural-language query")
    parser.add_argument("--vault", type=Path, default=Path("."), help="vault root (default: current directory)")
    parser.add_argument("--index", type=Path, help="index file (default: .vault-meta/retrieval/bm25.json)")
    parser.add_argument("--limit", type=int, default=10, help="maximum candidate pages (default: 10)")
    parser.add_argument("--rerank", action="store_true", help="opt in to local Ollama embedding reranking; failures fall back to BM25")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434", help="local Ollama base URL (loopback only; default: http://127.0.0.1:11434)")
    parser.add_argument("--ollama-model", default="nomic-embed-text", help="already-installed embedding model (no download is attempted)")
    parser.add_argument("--timeout", type=float, default=8.0, help="Ollama request timeout in seconds (default: 8)")
    args = parser.parse_args()
    index = args.index or args.vault / DEFAULT_INDEX
    try:
        data = _index_module.load_index(index)
        vault = args.vault.resolve()
        results = _index_module.query(data, args.query)
        results = [(page, score) for page, score in results
                   if _index_module.eligible(vault / Path(page), vault)]
        date = DATE.search(args.query)
        matched = set()
        if date:
            topic = _index_module.tokens(DATE.sub(" ", args.query))
            filename = "-".join(topic + [date.group()])
            matched = {page for page, _ in results if Path(page).stem == filename}
            if not matched and topic:
                heading = re.compile(r"(?m)^#{2,6}\s+[^\n]*" + re.escape(date.group()) + r"\b")
                for page, _ in results:
                    if data.get("titles", {}).get(page) == topic and heading.search((vault / page).read_text(encoding="utf-8", errors="replace")):
                        matched.add(page)
            results.sort(key=lambda item: (item[0] not in matched, -item[1], item[0]))
        results = results[:max(args.limit, 0)]
        if not results:
            print("No indexed matches; use wiki/index.md navigation.")
            return 0
        reranked = False
        if args.rerank and not date:
            try:
                results = rerank(args.query, results, vault, args.ollama_url,
                                 args.ollama_model, args.timeout)
                reranked = True
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, urllib.error.URLError) as exc:
                print(f"Rerank unavailable ({exc}); using BM25 results.", file=__import__("sys").stderr)
        for rank, (page, score) in enumerate(results, 1):
            if reranked:
                print(f"{rank}. {page}\tcosine={score:.6f}")
            elif date and page in matched:
                print(f"{rank}. {page}\tbm25={score:.6f} exact_date_match")
            else:
                print(f"{rank}. {page}\t{score:.6f}")
        return 0
    except (OSError, ValueError, KeyError, TypeError, OverflowError, json.JSONDecodeError) as exc:
        print(f"Retrieval unavailable ({exc}); use wiki/index.md navigation.", file=__import__("sys").stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
