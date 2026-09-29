#!/usr/bin/env python3
"""Build a deterministic BM25 index over Markdown pages under wiki/."""
import argparse
import json
import math
import os
import re
from pathlib import Path

TOKEN = re.compile(r"[\w]+", re.UNICODE)
HEADING = re.compile(r"(?m)^#\s+(.+?)\s*#*\s*$")
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


def build(root=Path("."), output=None):
    root = Path(root).resolve()
    output = safe_output(root, output if output is not None else DEFAULT_INDEX)
    documents = {}
    titles = {}
    wiki = root / "wiki"
    if wiki.is_symlink() or not wiki.is_dir():
        raise FileNotFoundError(f"wiki directory is missing or symlinked: {wiki}")
    for path in sorted(wiki.rglob("*.md")):
        if not eligible(path, root):
            continue
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        words = tokens(text)
        if words:
            documents[rel] = words
            heading = HEADING.search(text)
            if heading:
                titles[rel] = tokens(heading.group(1))
    postings = {}
    lengths = {}
    for page, words in documents.items():
        lengths[page] = len(words)
        frequencies = {}
        for word in words:
            frequencies[word] = frequencies.get(word, 0) + 1
        for word, count in frequencies.items():
            postings.setdefault(word, {})[page] = count
    data = {"version": 1, "documents": len(documents), "lengths": lengths, "terms": postings, "titles": titles}
    temp_name = output.name + ".tmp"
    output_name = output.name
    if output.is_symlink() or output.with_name(temp_name).is_symlink():
        raise ValueError("refusing symlinked index or temporary output")
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    parent_fd, parent_path = open_output_parent(root, output)
    temp_created = False
    temp_identity = None
    try:
        try:
            initial_output = os.stat(output_name, dir_fd=parent_fd, follow_symlinks=False)
            output_identity = (initial_output.st_dev, initial_output.st_ino)
        except FileNotFoundError:
            output_identity = None
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
        actual = os.stat(parent_path, follow_symlinks=False)
        opened = os.fstat(parent_fd)
        if (actual.st_dev, actual.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError(f"index parent changed during build: {parent_path}")
        try:
            current_output = os.stat(output_name, dir_fd=parent_fd, follow_symlinks=False)
            current_identity = (current_output.st_dev, current_output.st_ino)
        except FileNotFoundError:
            current_identity = None
        if current_identity != output_identity:
            raise ValueError("index output changed during build; refusing to replace concurrent update")
        os.replace(temp_name, output_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
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
    titles = data.get("titles", {})
    if not isinstance(titles, dict) or any(page not in lengths or not isinstance(words, list) or any(not isinstance(word, str) for word in words) for page, words in titles.items()):
        raise ValueError("malformed titles")
    return data


def query(data, text):
    terms = tokens(text)
    if not terms:
        return []
    lengths, postings = data["lengths"], data["terms"]
    titles = data.get("titles", {})
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
    args = parser.parse_args()
    try:
        count = build(args.vault, args.index)
    except (OSError, UnicodeError, ValueError) as exc:
        parser.exit(1, f"bm25-index: {exc}\n")
    print(f"Indexed {count} Markdown pages")


if __name__ == "__main__":
    main()
