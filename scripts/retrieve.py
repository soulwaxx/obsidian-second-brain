#!/usr/bin/env python3
"""Rank local wiki Markdown with BM25; optionally search cached semantic chunks."""
import argparse
import hashlib
import ipaddress
import json
import math
import re
from pathlib import Path
import urllib.parse
import urllib.request

from module_loading import load_source

_index_module = load_source("bm25_index", str(Path(__file__).with_name("bm25-index.py")))
DEFAULT_INDEX = Path(".vault-meta/retrieval/bm25.json")
DEFAULT_CHUNKS = Path(".vault-meta/retrieval/chunks.json")
MAX_DOCUMENT_CHARS = 8000
MAX_QUERY_CHARS = 4000
MAX_SEMANTIC_CANDIDATES = 100
RRF_K = 60
DATE = re.compile(r"(?<!\d)\d{4}-\d{2}(?:-\d{2})?(?!\d)")
HEADING = re.compile(r"(?m)^(#{2,6})\s+([^\n]+)$")
TOKEN = re.compile(r"[\w]+", re.UNICODE)


def dated_matches(data, results, query, date, vault):
    """Return exact dated notes and canonical pages with relevant dated sections."""
    terms = {word.casefold() for word in TOKEN.findall(DATE.sub(" ", query))}
    query_terms = [word.casefold() for word in TOKEN.findall(DATE.sub(" ", query))]
    titles = data.get("titles", {})
    matching_stems = {}
    for page, _ in results:
        stem = Path(page).stem
        if stem.endswith("-" + date.group()):
            stem_terms = [word.casefold() for word in TOKEN.findall(stem[:-len(date.group()) - 1])]
            if stem_terms:
                if len(stem_terms) <= len(query_terms) and query_terms[:len(stem_terms)] == stem_terms:
                    matching_stems[page] = stem_terms
    most_specific_stem = max((len(stem) for stem in matching_stems.values()), default=0)
    exact = {page for page, stem in matching_stems.items() if len(stem) == most_specific_stem}
    canonical = set()
    for page, _ in results:
        if page in exact:
            continue
        title = titles.get(page, [])
        if not title or len(title) > len(query_terms) or query_terms[:len(title)] != title:
            continue
        qualifiers = terms - set(title)
        text = (vault / page).read_text(encoding="utf-8", errors="replace")
        headings = list(HEADING.finditer(text))
        for index, heading in enumerate(headings):
            if not DATE.search(heading.group(2)) or date.group() not in heading.group(2):
                continue
            end = len(text)
            for following in headings[index + 1:]:
                if len(following.group(1)) <= len(heading.group(1)):
                    end = following.start()
                    break
            section = text[heading.start():end]
            section_terms = {word.casefold() for word in TOKEN.findall(section)}
            if not qualifiers or qualifiers & section_terms:
                canonical.add(page)
                break
    return exact, canonical


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _validate_request_url(value):
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Ollama request must use a plain loopback http URL without credentials")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("Ollama request has an invalid port") from exc
    try:
        local = parsed.hostname.casefold() == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        local = parsed.hostname.casefold() == "localhost"
    if not local:
        raise ValueError("Ollama request origin must be localhost or a loopback IP")
    return value


def local_endpoint(value):
    return _validate_request_url(value).rstrip("/") + "/api/embed"


def embeddings(inputs, endpoint, model, timeout):
    endpoint = _validate_request_url(endpoint)
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


def _safe_cache_path(vault):
    path = vault / DEFAULT_CHUNKS
    if any((vault / part).is_symlink() for part in (".vault-meta", ".vault-meta/retrieval")) or path.is_symlink():
        raise ValueError("semantic cache path contains a symlink")
    return path


def _valid_semantic_pages(vault, model, endpoint, timeout):
    """Load only well-formed vectors whose page and exact source spans are current."""
    from module_loading import load_source
    chunk_module = load_source("retrieve_semantic_chunks", str(Path(__file__).with_name("semantic-chunks.py")))
    path = _safe_cache_path(vault)
    if not path.is_file():
        return chunk_module, {}
    cache = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cache, dict) or cache.get("version") != chunk_module.CACHE_VERSION or not isinstance(cache.get("pages"), dict):
        raise ValueError("unsupported or malformed semantic cache")
    try:
        endpoint = chunk_module.local_endpoint(endpoint)
        model_digest = chunk_module.model_digest(endpoint, model, timeout=timeout)
    except Exception:
        model_digest = None
    if model_digest is None:
        # Unverified identity cannot establish that cached document vectors still
        # match the installed model, even if a query embedding can be obtained.
        return chunk_module, {}
    pages = {}
    for relative, entry in sorted(cache["pages"].items()):
        rel = Path(relative)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0] != "wiki":
            continue
        source_path = vault / rel
        if not _index_module.eligible(source_path, vault) or not isinstance(entry, dict):
            continue
        if entry.get("model") != model:
            continue
        if model_digest is not None and entry.get("modelDigest") != model_digest:
            continue
        settings = entry.get("settings")
        if (not isinstance(settings, dict) or settings.get("algorithm") != chunk_module.ALGORITHM
                or type(settings.get("maxChars")) is not int or type(settings.get("targetChars")) is not int
                or settings["maxChars"] < 64 or settings["targetChars"] < 1
                or settings["targetChars"] > settings["maxChars"]):
            continue
        try:
            source = source_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        if entry.get("contentHash") != digest or not isinstance(entry.get("chunks"), list):
            continue
        good_chunks = []
        for chunk in entry["chunks"]:
            if not isinstance(chunk, dict):
                continue
            start, end, vector = chunk.get("start"), chunk.get("end"), chunk.get("vector")
            if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(source)
                    or end - start > settings["maxChars"]):
                continue
            body = source[start:end]
            if chunk.get("sourceHash") != hashlib.sha256(body.encode("utf-8")).hexdigest():
                continue
            if not isinstance(chunk.get("text"), str) or not isinstance(vector, list) or not vector:
                continue
            if len(vector) > 16384 or any(not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) for value in vector):
                continue
            good_chunks.append({"text": chunk["text"], "start": start, "end": end,
                                "vector": [float(value) for value in vector], "source": source,
                                "pageHash": digest})
        if good_chunks:
            pages[relative] = good_chunks
    return chunk_module, pages


def _cosine(left, right):
    if len(left) != len(right):
        return None
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


def semantic_candidates(query, vault, endpoint, model, timeout):
    """Return deterministic semantic chunk ranks; all failures are handled by caller."""
    chunk_module, pages = _valid_semantic_pages(vault, model, endpoint, timeout)
    if not pages:
        return []
    url = chunk_module.local_endpoint(endpoint)
    query_vector = chunk_module.embeddings([query[:MAX_QUERY_CHARS]], url, model, timeout)[0]
    if not any(query_vector):
        raise ValueError("Ollama returned a zero query embedding")
    candidates = []
    for page, chunks in pages.items():
        source_path = vault / page
        if not _index_module.eligible(source_path, vault):
            continue
        try:
            current_source = source_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if hashlib.sha256(current_source.encode("utf-8")).hexdigest() != chunks[0]["pageHash"]:
            continue
        for chunk_index, chunk in enumerate(chunks):
            similarity = _cosine(query_vector, chunk["vector"])
            if similarity is not None:
                candidates.append((page, similarity, chunk_index, chunk))
    candidates.sort(key=lambda item: (-item[1], item[0], item[2]))
    pages_seen = set()
    grouped = []
    for candidate in candidates:
        if candidate[0] in pages_seen:
            continue
        pages_seen.add(candidate[0])
        grouped.append(candidate)
        if len(grouped) >= MAX_SEMANTIC_CANDIDATES:
            break
    return grouped


def hybrid_results(query, data, vault, lexical, endpoint, model, timeout, limit):
    """Rank union of lexical and semantic pages with reciprocal-rank fusion."""
    semantic = semantic_candidates(query, vault, endpoint, model, timeout)
    lexical_rank = {page: rank for rank, (page, _) in enumerate(lexical, 1)}
    semantic_rank = {}
    page_chunks = {}
    for rank, (page, score, chunk_index, chunk) in enumerate(semantic, 1):
        if page not in semantic_rank:
            semantic_rank[page] = (rank, score, chunk_index, chunk)
    for page in sorted(set(lexical_rank) | set(semantic_rank)):
        if page not in lexical_rank and page not in semantic_rank:
            continue
        semantic_info = semantic_rank.get(page)
        fused = (1 / (RRF_K + lexical_rank[page]) if page in lexical_rank else 0.0)
        if semantic_info:
            fused += 1 / (RRF_K + semantic_info[0])
        page_chunks[page] = (fused, semantic_info)
    ranked = sorted(page_chunks, key=lambda page: (
        -page_chunks[page][0],
        page_chunks[page][1][0] if page_chunks[page][1] else MAX_SEMANTIC_CANDIDATES + 1,
        lexical_rank.get(page, MAX_SEMANTIC_CANDIDATES + 1), page))
    output = []
    for page in ranked[:max(0, limit)]:
        fused, semantic_info = page_chunks[page]
        if semantic_info:
            _, similarity, _, chunk = semantic_info
            start, end = chunk["start"], chunk["end"]
            source = chunk["source"]
            line_start = source.count("\n", 0, start) + 1
            line_end = source.count("\n", 0, end) + (1 if end == 0 or source[end - 1:end] != "\n" else 0)
            excerpt = " ".join(source[start:end].split())
            if len(excerpt) > 360:
                excerpt = excerpt[:359].rstrip() + "…"
            output.append({"page": page, "score": fused, "semanticScore": similarity,
                           "start": start, "end": end, "lineStart": line_start,
                           "lineEnd": max(line_start, line_end), "excerpt": excerpt,
                           "match": "hybrid" if page in lexical_rank else "semantic"})
        else:
            path = vault / page
            text = path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines() or [""]
            output.append({"page": page, "score": fused, "semanticScore": None,
                           "start": 0, "end": 0, "lineStart": 1, "lineEnd": min(3, len(lines)),
                           "excerpt": " ".join(lines[:3])[:360], "match": "bm25"})
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", help="natural-language query")
    parser.add_argument("--vault", type=Path, default=Path("."), help="vault root (default: current directory)")
    parser.add_argument("--index", type=Path, help="index file (default: .vault-meta/retrieval/bm25.json)")
    parser.add_argument("--limit", type=int, default=10, help="maximum candidate pages (default: 10)")
    parser.add_argument("--rerank", action="store_true", help="opt in to local Ollama embedding reranking; failures fall back to BM25")
    parser.add_argument("--hybrid", action="store_true", help="opt in to cached semantic chunk + BM25 rank fusion")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434", help="local Ollama base URL (loopback only; default: http://127.0.0.1:11434)")
    parser.add_argument("--ollama-model", default="qwen3-embedding:4b", help="already-installed embedding model (no download is attempted)")
    parser.add_argument("--timeout", type=float, default=8.0, help="Ollama request timeout in seconds (default: 8)")
    args = parser.parse_args()
    index = args.index or args.vault / DEFAULT_INDEX
    try:
        data = _index_module.load_index(index)
        vault = args.vault.resolve()
        results = [(page, score) for page, score in _index_module.query(data, args.query)
                   if _index_module.eligible(vault / Path(page), vault)]
        date = DATE.search(args.query)
        matched = set()
        if date:
            exact, canonical = dated_matches(data, results, args.query, date, vault)
            matched = exact | canonical
            results.sort(key=lambda item: (0 if item[0] in exact else 1 if item[0] in canonical else 2, -item[1], item[0]))
        reranked = False
        if (args.hybrid or args.rerank) and not date:
            try:
                if args.hybrid:
                    hybrid = hybrid_results(args.query, data, vault, results, args.ollama_url,
                                            args.ollama_model, args.timeout, args.limit)
                    if hybrid:
                        for rank, item in enumerate(hybrid, 1):
                            print(f"{rank}. {item['page']}\t{item['score']:.6f}\t{item['match']}\tlines={item['lineStart']}-{item['lineEnd']} chars={item['start']}-{item['end']}\t{item['excerpt']}")
                        return 0
                else:
                    results = rerank(args.query, results[:max(args.limit, 0)], vault, args.ollama_url,
                                     args.ollama_model, args.timeout)
                    reranked = True
            except Exception as exc:
                print(f"Semantic search unavailable ({exc}); using BM25 results.", file=__import__("sys").stderr)
        results = results[:max(args.limit, 0)]
        if not results:
            print("No indexed matches; use wiki/index.md navigation.")
            return 0
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
