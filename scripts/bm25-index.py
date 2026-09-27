#!/usr/bin/env python3
"""Build a deterministic BM25 index over Markdown pages under wiki/."""
import argparse
import json
import math
import os
import re
from pathlib import Path

TOKEN = re.compile(r"[\w]+", re.UNICODE)
DEFAULT_INDEX = Path(".vault-meta/retrieval/bm25.json")
RESERVED = {"index.md", "log.md", "_plan.md"}
MAX_COUNT = 2**31 - 1


def eligible(path, root):
    root = Path(root).resolve()
    wiki = root / "wiki"
    if wiki.is_symlink():
        return False
    try:
        rel = path.relative_to(wiki)
        resolved = path.resolve(strict=True)
        resolved.relative_to(wiki.resolve(strict=True))
    except (OSError, ValueError):
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


def tokens(text):
    return [word.casefold() for word in TOKEN.findall(text)]


def build(root=Path("."), output=None):
    root = Path(root).resolve()
    output = safe_output(root, output if output is not None else DEFAULT_INDEX)
    documents = {}
    wiki = root / "wiki"
    if wiki.is_symlink() or not wiki.is_dir():
        raise FileNotFoundError(f"wiki directory is missing or symlinked: {wiki}")
    for path in sorted(wiki.rglob("*.md")):
        if not eligible(path, root):
            continue
        rel = path.relative_to(root).as_posix()
        words = tokens(path.read_text(encoding="utf-8", errors="replace"))
        if words:
            documents[rel] = words
    postings = {}
    lengths = {}
    for page, words in documents.items():
        lengths[page] = len(words)
        frequencies = {}
        for word in words:
            frequencies[word] = frequencies.get(word, 0) + 1
        for word, count in frequencies.items():
            postings.setdefault(word, {})[page] = count
    data = {"version": 1, "documents": len(documents), "lengths": lengths, "terms": postings}
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    if temp.is_symlink() or output.is_symlink():
        raise ValueError("refusing symlinked index or temporary output")
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    temp_created = False
    try:
        with temp.open("x", encoding="utf-8") as stream:
            temp_created = True
            stream.write(payload)
        temp.replace(output)
    except BaseException:
        if temp_created:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass
        raise
    return len(documents)


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
    return data


def query(data, text):
    terms = tokens(text)
    if not terms:
        return []
    lengths, postings = data["lengths"], data["terms"]
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
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "update"), help="build or fully regenerate the local index (update is an alias)")
    parser.add_argument("--vault", type=Path, default=Path("."), help="vault root (default: current directory)")
    parser.add_argument("--index", type=Path, help="index file under .vault-meta/retrieval/ (default: .vault-meta/retrieval/bm25.json)")
    args = parser.parse_args()
    try:
        count = build(args.vault, args.index)
    except (OSError, UnicodeError, ValueError) as exc:
        parser.exit(1, f"bm25-index: {exc}\n")
    print(f"Indexed {count} Markdown pages")


if __name__ == "__main__":
    main()
