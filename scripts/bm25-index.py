#!/usr/bin/env python3
"""Explicitly build current/historical BM25 views with original source hashes."""
import argparse
import hashlib
import json
import math
import os
import re
from pathlib import Path

import retrieval_projection as projection

TOKEN = re.compile(r"[\w]+", re.UNICODE)
HEADING = re.compile(r"(?m)^#\s+(.+?)\s*#*\s*$")
DEFAULT_INDEX = Path(".vault-meta/retrieval/bm25.json")
RESERVED = {"index.md", "log.md", "_plan.md"}
MAX_COUNT = 2**31 - 1


def eligible(path, root, *, discovery=False):
    root = Path(root).resolve()
    wiki = root / "wiki"
    if wiki.is_symlink():
        return False
    try:
        rel = path.relative_to(wiki)
        resolved = path.resolve(strict=True)
        resolved.relative_to(wiki.resolve(strict=True))
    except ValueError:
        return False
    except OSError:
        if discovery:
            raise
        return False
    return (not path.is_symlink()
            and path.is_file()
            and path.name.casefold() not in RESERVED
            and not any(part.startswith(".") for part in rel.parts))


def safe_output(root, output):
    root = Path(root).resolve()
    output = Path(output)
    if not output.is_absolute():
        output = root / output
    try:
        rel = output.relative_to(root)
    except ValueError as exc:
        raise ValueError("index output must be inside the vault") from exc
    if ".." in rel.parts:
        raise ValueError("index output must not traverse outside the vault")
    if rel.parts[:2] != (".vault-meta", "retrieval") or len(rel.parts) < 3:
        raise ValueError("index output must be inside .vault-meta/retrieval/")
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"refusing symlinked index path: {current}")
    return output


def open_output_parent(root, output):
    """Create/open cache parents without following a swapped directory or symlink."""
    rel = output.relative_to(root)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(root, flags)
    current = root
    try:
        for part in rel.parts[:-1]:
            current = current / part
            try:
                os.mkdir(part, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        actual = os.stat(current, follow_symlinks=False)
        opened = os.fstat(fd)
        if (actual.st_dev, actual.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError(f"index parent changed during build: {current}")
        return fd, current
    except BaseException:
        os.close(fd)
        raise


def tokens(text):
    return [word.casefold() for word in TOKEN.findall(text)]


def markdown_sources(root):
    """Walk visible wiki directories without suppressing traversal errors."""
    wiki = Path(root) / "wiki"
    if wiki.is_symlink() or not wiki.is_dir():
        raise FileNotFoundError(f"wiki directory is missing or symlinked: {wiki}")
    found = []

    def walk(directory):
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    walk(path)
                elif entry.is_file(follow_symlinks=False) and entry.name.casefold().endswith(".md"):
                    if eligible(path, root, discovery=True):
                        found.append(path)

    walk(wiki)
    return sorted(found)


def source_manifest(root):
    """Hashes of all eligible original Markdown sources, including empty pages."""
    root = Path(root).resolve()
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in markdown_sources(root)}


def output_identity(output):
    try:
        info = output.lstat()
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    except FileNotFoundError:
        return None


def build(root=Path("."), output=None):
    root = Path(root).resolve()
    output = safe_output(root, output if output is not None else DEFAULT_INDEX)
    initial_identity = output_identity(output)
    source_hashes = source_manifest(root)
    documents = {}
    history = {}
    titles = {}
    historical_titles = {}
    for path in markdown_sources(root):
        rel = path.relative_to(root).as_posix()
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != source_hashes.get(rel):
            raise ValueError("index source changed while reading")
        text = raw.decode("utf-8", errors="replace")
        historical_words = tokens(text)
        if historical_words:
            history[rel] = historical_words
            heading = HEADING.search(text)
            if heading:
                historical_titles[rel] = tokens(heading.group(1))
        projected = projection.current(text)
        words = tokens(projected)
        if words:
            documents[rel] = words
            heading = HEADING.search(projected)
            if heading:
                titles[rel] = tokens(heading.group(1))
    lengths, postings = document_postings(documents)
    historical_lengths, historical_postings = document_postings(history)
    data = {"version": 1, "documents": len(documents), "lengths": lengths, "terms": postings, "titles": titles,
            "sourceHashes": source_hashes, "projectionScheme": projection.SCHEME,
            "historicalLengths": historical_lengths, "historicalTerms": historical_postings, "historicalTitles": historical_titles}
    return publish_index(root, output, data, source_hashes, initial_identity)


def document_postings(documents):
    postings = {}
    lengths = {}
    for page, words in documents.items():
        lengths[page] = len(words)
        frequencies = {}
        for word in words:
            frequencies[word] = frequencies.get(word, 0) + 1
        for word, count in frequencies.items():
            postings.setdefault(word, {})[page] = count
    return lengths, postings


def publish_index(root, output, data, source_hashes, initial_identity):
    temp_name = output.name + ".tmp"
    output_name = output.name
    if output.is_symlink() or output.with_name(temp_name).is_symlink():
        raise ValueError("refusing symlinked index or temporary output")
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    parent_fd, parent_path = open_output_parent(root, output)
    temp_created = False
    temp_identity = None
    try:
        # Recheck through the pinned directory, then ensure the path still names it.
        actual = os.stat(parent_path, follow_symlinks=False)
        opened = os.fstat(parent_fd)
        if (actual.st_dev, actual.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError(f"index parent changed during build: {parent_path}")
        fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o666, dir_fd=parent_fd)
        temp_created = True
        temp_stat = os.fstat(fd)
        temp_identity = (temp_stat.st_dev, temp_stat.st_ino)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        actual = os.stat(parent_path, follow_symlinks=False)
        opened = os.fstat(parent_fd)
        if (actual.st_dev, actual.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError(f"index parent changed during build: {parent_path}")
        try:
            current_output = os.stat(output_name, dir_fd=parent_fd, follow_symlinks=False)
            current_identity = (current_output.st_dev, current_output.st_ino, current_output.st_size, current_output.st_mtime_ns)
        except FileNotFoundError:
            current_identity = None
        if current_identity != initial_identity:
            raise ValueError("index output changed during build; refusing to replace concurrent update")
        if source_manifest(root) != source_hashes:
            raise ValueError("index source changed during build; refusing stale publication")
        os.replace(temp_name, output_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    except BaseException:
        if temp_created:
            try:
                current_temp = os.stat(temp_name, dir_fd=parent_fd, follow_symlinks=False)
                if temp_identity == (current_temp.st_dev, current_temp.st_ino):
                    os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        raise
    finally:
        os.close(parent_fd)
    return data["documents"]


def load_index(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    lengths, terms = data["lengths"], data["terms"]
    if data.get("version") != 1 or not isinstance(lengths, dict) or not isinstance(terms, dict):
        raise ValueError("unsupported or malformed index")
    if type(data.get("documents")) is not int or data["documents"] > MAX_COUNT:
        raise ValueError("malformed document count")
    if data["documents"] != len(lengths) or any(type(n) is not int or n <= 0 or n > MAX_COUNT for n in lengths.values()):
        raise ValueError("malformed document lengths")
    for word, entries in terms.items():
        if not isinstance(word, str) or not isinstance(entries, dict) or any(p not in lengths or type(n) is not int or n <= 0 or n > MAX_COUNT for p, n in entries.items()):
            raise ValueError("malformed postings")
    if "projectionScheme" in data:
        if data["projectionScheme"] != projection.SCHEME:
            raise ValueError("unsupported retrieval projection")
        history_lengths, history_terms = data.get("historicalLengths"), data.get("historicalTerms")
        history_titles = data.get("historicalTitles", {})
        if (not isinstance(history_lengths, dict) or not isinstance(history_titles, dict) or any(page not in history_lengths or not isinstance(words, list)
                or any(not isinstance(word, str) for word in words) for page, words in history_titles.items())):
            raise ValueError("malformed historical titles")
        if (not isinstance(history_lengths, dict) or not isinstance(history_terms, dict)
                or any(type(n) is not int or not 0 < n <= MAX_COUNT for n in history_lengths.values())
                or any(not isinstance(word, str) or not isinstance(entries, dict)
                       or any(p not in history_lengths or type(n) is not int or not 0 < n <= MAX_COUNT for p, n in entries.items())
                       for word, entries in history_terms.items())):
            raise ValueError("malformed historical postings")
    source_hashes = data.get("sourceHashes")
    if source_hashes is not None and (not isinstance(source_hashes, dict)
            or any(not isinstance(page, str) or not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                   for page, value in source_hashes.items())
            or not set(lengths) <= set(source_hashes)):
        raise ValueError("malformed source hashes")
    titles = data.get("titles", {})
    if not isinstance(titles, dict) or any(page not in lengths or not isinstance(words, list) or any(not isinstance(word, str) for word in words) for page, words in titles.items()):
        raise ValueError("malformed titles")
    return data


def query(data, text):
    terms = tokens(text)
    if not terms:
        return []
    historical = bool(projection.ISO_DATE.search(text)) and "historicalLengths" in data
    lengths = data["historicalLengths"] if historical else data["lengths"]
    postings = data["historicalTerms"] if historical else data["terms"]
    titles = data.get("historicalTitles", {}) if historical else data.get("titles", {})
    avg = sum(lengths.values()) / max(len(lengths), 1)
    scores = {}
    k1, b = 1.5, 0.75
    for term in terms:
        found = postings.get(term, {})
        df = len(found)
        if not df:
            continue
        idf = math.log(1 + (len(lengths) - df + 0.5) / (df + 0.5))
        for page, freq in found.items():
            norm = freq + k1 * (1 - b + b * lengths[page] / avg)
            scores[page] = scores.get(page, 0.0) + idf * freq * (k1 + 1) / norm
    title_bonus = max(scores.values(), default=0.0)
    for page, title in titles.items():
        if title == terms and page in scores:
            scores[page] += title_bonus
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "update"), help="build or fully regenerate the local index (update is an alias)")
    parser.add_argument("--vault", type=Path, default=Path("."), help="vault root (default: current directory)")
    parser.add_argument("--index", type=Path, help="index file under .vault-meta/retrieval/ (default: .vault-meta/retrieval/bm25.json)")
    parser.add_argument("--semantic-chunks", action="store_true", help="also explicitly build optional local semantic chunk cache")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434", help="loopback Ollama URL for explicit semantic chunk builds")
    parser.add_argument("--ollama-model", default="qwen3-embedding:4b", help="already-installed embedding model; no download is attempted")
    parser.add_argument("--timeout", type=float, default=8.0, help="embedding request timeout in seconds")
    parser.add_argument("--chunk-max-chars", type=int, default=1800, help="maximum source characters per semantic chunk")
    parser.add_argument("--chunk-target-chars", type=int, default=1000, help="target source characters per semantic chunk")
    args = parser.parse_args()
    try:
        root = args.vault.resolve()
        from wiki_lifecycle import retrieval_status, acknowledge_retrieval
        token = retrieval_status(root).get("token")
        count = build(root, args.index)
        if token and safe_output(root, args.index or DEFAULT_INDEX) == root / DEFAULT_INDEX:
            acknowledge_retrieval(root, token)
    except (OSError, UnicodeError, ValueError) as exc:
        parser.exit(1, f"bm25-index: {exc}\n")
    print(f"Indexed {count} Markdown pages")
    if args.semantic_chunks:
        try:
            from module_loading import load_source
            module = load_source("semantic_chunks", str(Path(__file__).with_name("semantic-chunks.py")))
            result = module.build(args.vault, args.ollama_url, args.ollama_model,
                                  timeout=args.timeout, max_chars=args.chunk_max_chars,
                                  target_chars=args.chunk_target_chars)
            print(f"Semantic chunks: indexed {result['indexed']}, reused {result['reused']}, deferred {len(result['deferred'])}; "
                  f"embedded {result['embeddedInputs']} inputs/{result['embeddedChars']} chars, reused {result['reusedInputs']} inputs; "
                  f"{result['buildSeconds']:.3f}s, {result['cacheBytes']} cache bytes")
        except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
            print(f"Semantic chunks unavailable ({exc}); page-level BM25 remains available", file=__import__("sys").stderr)



if __name__ == "__main__":
    main()
