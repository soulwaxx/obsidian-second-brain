#!/usr/bin/env python3
"""Explicit local semantic chunk/cache builder.

API for retrieval consumers: ``build(vault, endpoint, model, ...)`` writes
``.vault-meta/retrieval/chunks.json``. Cache schema v1 has a ``pages`` map keyed
by vault-relative Markdown path. Entries contain page ``contentHash``, model
name/digest, algorithm/settings, and ordered chunks. Each chunk contains
context-prefixed ``text``, original-page ``start``/``end`` character offsets,
``sourceHash``, and an embedding vector. A missing entry means no current
semantic evidence; callers must fall back to page BM25. Builds are explicit and
never install/download models. ``text_units``, ``semantic_chunks`` and
``heading_chunks`` are also stable focused-test/benchmark interfaces.
"""
import hashlib
import ipaddress
import json
import math
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

from module_loading import load_source

_INDEX = load_source("bm25_index_for_chunks", str(Path(__file__).with_name("bm25-index.py")))
CACHE_VERSION = 1
ALGORITHM = "semantic-adjacent-cosine-v1"
DEFAULT_CACHE = Path(".vault-meta/retrieval/chunks.json")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^\s{0,3}(```+|~~~+)")
SENTENCE = re.compile(r"(?<=[.!?])\s+")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validate_request_url(value):
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("embedding request must use a plain loopback http URL without credentials")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("embedding request has an invalid port") from exc
    try:
        local = parsed.hostname.casefold() == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        local = parsed.hostname.casefold() == "localhost"
    if not local:
        raise ValueError("embedding request origin must be localhost or a loopback IP")
    return value


def local_endpoint(value):
    return _validate_request_url(value).rstrip("/")


def _request(endpoint, route, payload=None, timeout=8.0):
    url = _validate_request_url(endpoint + route)
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"} if data else {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _canonical_model_name(name):
    """Treat an untagged model's final path component as Ollama's :latest tag."""
    if not isinstance(name, str) or not name:
        return None
    terminal = name.rsplit("/", 1)[-1]
    return name if ":" in terminal else name + ":latest"


def model_digest(endpoint, model, timeout=8.0):
    """Return the digest for an installed model, matching Ollama's :latest aliases."""
    requested = _canonical_model_name(model)
    if requested is None:
        return None
    try:
        data = _request(endpoint, "/api/tags", timeout=timeout)
        models = data.get("models", [])
        for entry in models:
            if not isinstance(entry, dict):
                continue
            names = (entry.get("name"), entry.get("model"))
            if any(_canonical_model_name(name) == requested for name in names):
                value = entry.get("digest")
                return value if isinstance(value, str) and value else None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return None


def embeddings(inputs, endpoint, model, timeout=8.0):
    if not inputs:
        return []
    data = _request(endpoint, "/api/embed", {"model": model, "input": inputs}, timeout)
    vectors = data["embeddings"]
    if not isinstance(vectors, list) or len(vectors) != len(inputs):
        raise ValueError("embedding service returned an invalid embedding count")
    result = []
    dimension = None
    for vector in vectors:
        if not isinstance(vector, list) or not vector:
            raise ValueError("embedding service returned an invalid vector")
        if dimension is None:
            dimension = len(vector)
        if len(vector) != dimension or any(not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x) for x in vector):
            raise ValueError("embedding service returned malformed vector values")
        result.append([float(x) for x in vector])
    return result


def _split_oversized(text, start, max_chars):
    """Split long paragraphs on sentence/line boundaries, retaining exact offsets."""
    units = []
    cursor = 0
    while cursor < len(text):
        end = min(cursor + max_chars, len(text))
        if end < len(text):
            candidates = [match.end() for match in SENTENCE.finditer(text, cursor, end)]
            if candidates:
                end = candidates[-1]
            else:
                newline = text.rfind("\n", cursor, end)
                if newline > cursor:
                    end = newline + 1
        if end <= cursor:
            end = min(cursor + max_chars, len(text))
        units.append({"start": start + cursor, "end": start + end, "text": text[cursor:end]})
        cursor = end
    return units


def text_units(text, max_chars=1200):
    """Produce ordered paragraph/heading/code-block atoms with original character offsets."""
    if max_chars < 64:
        raise ValueError("max_chars must be at least 64")
    lines = text.splitlines(keepends=True)
    offsets = []
    position = 0
    for line in lines:
        offsets.append(position)
        position += len(line)
    units = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        begin = index
        fence = FENCE.match(line)
        heading = HEADING.match(line.rstrip("\r\n"))
        if fence:
            marker = fence.group(1)
            index += 1
            while index < len(lines):
                current = lines[index]
                index += 1
                closing = FENCE.match(current)
                if closing and closing.group(1)[0] == marker[0] and len(closing.group(1)) >= len(marker):
                    break
        elif heading:
            index += 1
        else:
            index += 1
            while index < len(lines) and lines[index].strip() and not HEADING.match(lines[index].rstrip("\r\n")) and not FENCE.match(lines[index]):
                index += 1
        start = offsets[begin]
        end = offsets[index] if index < len(offsets) else len(text)
        value = text[start:end]
        if len(value) > max_chars:
            units.extend(_split_oversized(value, start, max_chars))
        else:
            units.append({"start": start, "end": end, "text": value})
    return units


def _cosine(left, right):
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


def _markdown_headings(source):
    """Yield real Markdown headings, ignoring heading-like lines inside code fences."""
    position = 0
    fence = None
    for line in source.splitlines(keepends=True):
        marker = FENCE.match(line)
        if marker:
            value = marker.group(1)
            if fence is None:
                fence = value
            elif value[0] == fence[0] and len(value) >= len(fence):
                fence = None
            position += len(line)
            continue
        if fence is None:
            heading = HEADING.match(line.rstrip("\r\n"))
            if heading:
                yield position, heading.group(1), heading.group(2)
        position += len(line)


def _context(source, relpath, start, end):
    title = Path(relpath).stem.replace("-", " ")
    headings = list(_markdown_headings(source))
    for _, depth, value in headings:
        if len(depth) == 1:
            title = value
            break
    active = []
    for position, hashes, value in headings:
        if position >= start:
            break
        level = len(hashes)
        active = [(depth, heading) for depth, heading in active if depth < level]
        if level > 1:
            active.append((level, value))
    return title, [value for _, value in active]


def _fence_state(source, offset):
    state = None
    for line in source[:offset].splitlines():
        match = FENCE.match(line)
        if not match:
            continue
        marker = match.group(1)
        if state is None:
            state = marker
        elif marker[0] == state[0] and len(marker) >= len(state):
            state = None
    return state


def _render(relpath, source, start, end):
    title, headings = _context(source, relpath, start, end)
    prefix = f"Title: {title}\n"
    if headings:
        prefix += "Heading: " + " > ".join(headings) + "\n"
    opening = _fence_state(source, start)
    closing = _fence_state(source, end)
    body = source[start:end]
    if opening:
        body = opening + "\n" + body
    if closing:
        body += "\n" + closing[:3]
    return prefix + body


def _chunks(relpath, source, vectors, max_chars, target_chars, strategy):
    units = text_units(source, max_chars)
    if len(units) != len(vectors):
        raise ValueError("one embedding is required per text unit")
    groups = []
    first = 0
    while first < len(units):
        last = first + 1
        while last < len(units):
            length = units[last]["end"] - units[first]["start"]
            if length > max_chars:
                break
            if length >= target_chars:
                break
            last += 1
        if last < len(units) and units[last]["end"] - units[first]["start"] <= max_chars:
            # Choose the most semantically distinct adjacent pair near the target.
            lower = max(first + 1, last - max(3, (target_chars // max(1, max_chars // 4))))
            candidates = range(lower, last + 1)
            boundary = min(candidates, key=lambda idx: (
                _cosine(vectors[idx - 1], vectors[idx]),
                abs(units[idx - 1]["end"] - units[first]["start"] - target_chars), idx))
            last = boundary
        group = units[first:last]
        start, end = group[0]["start"], group[-1]["end"]
        body = source[start:end]
        rendered = _render(relpath, source, start, end)
        # Context metadata is intentionally outside source offsets, but also bounded.
        if len(rendered) > max_chars + 512:
            raise ValueError("chunk context exceeds bounded metadata allowance")
        groups.append({"text": rendered, "start": start, "end": end,
                       "sourceHash": sha256(body), "vector": _mean(vectors[first:last]),
                       "strategy": strategy})
        first = last
    return groups


def _mean(vectors):
    if not vectors:
        return []
    return [sum(vector[i] for vector in vectors) / len(vectors) for i in range(len(vectors[0]))]


def semantic_chunks(relpath, source, vectors, max_chars=1800, target_chars=1000):
    if target_chars < 1 or target_chars > max_chars:
        raise ValueError("target_chars must be between 1 and max_chars")
    dimension = None
    for vector in vectors:
        if not isinstance(vector, list) or not vector:
            raise ValueError("semantic chunk vectors must be nonempty lists")
        if dimension is None:
            dimension = len(vector)
        if len(vector) != dimension or any(not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) for value in vector):
            raise ValueError("semantic chunk vectors must have one finite shared dimension")
    return _chunks(relpath, source, vectors, max_chars, target_chars, ALGORITHM)


def heading_chunks(relpath, source, max_chars=1800):
    """Deterministic heading-aware baseline for benchmark comparison only."""
    units = text_units(source, max_chars)
    groups, first = [], 0
    for index, unit in enumerate(units):
        if index > first and HEADING.match(unit["text"].strip().splitlines()[0]):
            start, end = units[first]["start"], units[index - 1]["end"]
            groups.append((first, index, start, end))
            first = index
    if first < len(units):
        groups.append((first, len(units), units[first]["start"], units[-1]["end"]))
    chunks = []
    for _, _, start, end in groups:
        if end - start > max_chars:
            # Retain bounded chunks even where a heading section is long.
            subunits = [item for item in units if start <= item["start"] < end]
            chunk_start, chunk_end = subunits[0]["start"], subunits[0]["end"]
            for item in subunits[1:]:
                if item["end"] - chunk_start > max_chars:
                    body = source[chunk_start:chunk_end]
                    chunks.append({"text": _render(relpath, source, chunk_start, chunk_end), "start": chunk_start,
                                   "end": chunk_end, "sourceHash": sha256(body), "strategy": "heading-baseline-v1"})
                    chunk_start = item["start"]
                chunk_end = item["end"]
            end = chunk_end
            for chunk_start, chunk_end in [(chunk_start, end)]:
                body = source[chunk_start:chunk_end]
                chunks.append({"text": _render(relpath, source, chunk_start, chunk_end), "start": chunk_start,
                               "end": chunk_end, "sourceHash": sha256(body), "strategy": "heading-baseline-v1"})
        else:
            body = source[start:end]
            chunks.append({"text": _render(relpath, source, start, end), "start": start, "end": end,
                           "sourceHash": sha256(body), "strategy": "heading-baseline-v1"})
    return chunks


def _settings(max_chars, target_chars):
    return {"algorithm": ALGORITHM, "maxChars": max_chars, "targetChars": target_chars}


def _page_list(vault):
    wiki = vault / "wiki"
    if wiki.is_symlink() or not wiki.is_dir():
        raise FileNotFoundError(f"wiki directory is missing or symlinked: {wiki}")
    return [path for path in sorted(wiki.rglob("*"))
            if path.is_file() and path.name.casefold().endswith(".md") and _INDEX.eligible(path, vault)]


def _load_cache(path):
    if not path.exists():
        return {"version": CACHE_VERSION, "pages": {}}
    if path.is_symlink():
        raise ValueError("refusing symlinked semantic cache")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION or not isinstance(data.get("pages"), dict):
        raise ValueError("unsupported or malformed semantic cache")
    return data


def publish_cache(vault, data, output=DEFAULT_CACHE):
    vault = Path(vault).resolve()
    output = _INDEX.safe_output(vault, output)
    temp_name = output.name + ".tmp"
    if output.is_symlink() or output.with_name(temp_name).is_symlink():
        raise ValueError("refusing symlinked semantic cache or temporary output")
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    parent_fd, parent_path = _INDEX.open_output_parent(vault, output)
    made_temp = False
    identity = None
    try:
        try:
            original = os.stat(output.name, dir_fd=parent_fd, follow_symlinks=False)
            original_identity = (original.st_dev, original.st_ino)
        except FileNotFoundError:
            original_identity = None
        fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o666, dir_fd=parent_fd)
        made_temp = True
        stat = os.fstat(fd)
        identity = (stat.st_dev, stat.st_ino)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
        actual = os.stat(parent_path, follow_symlinks=False)
        opened = os.fstat(parent_fd)
        if (actual.st_dev, actual.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("semantic cache parent changed during build")
        try:
            current = os.stat(output.name, dir_fd=parent_fd, follow_symlinks=False)
            current_identity = (current.st_dev, current.st_ino)
        except FileNotFoundError:
            current_identity = None
        if current_identity != original_identity:
            raise ValueError("semantic cache changed during build; refusing concurrent replacement")
        os.replace(temp_name, output.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    except BaseException:
        if made_temp:
            try:
                current = os.stat(temp_name, dir_fd=parent_fd, follow_symlinks=False)
                if identity == (current.st_dev, current.st_ino):
                    os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        raise
    finally:
        os.close(parent_fd)


def build(vault, endpoint, model, timeout=8.0, max_chars=1800, target_chars=1000,
          output=DEFAULT_CACHE):
    """Build explicitly; missing service removes stale changed entries and defers them."""
    vault = Path(vault).resolve()
    endpoint = local_endpoint(endpoint)
    if not isinstance(model, str) or not model.strip():
        raise ValueError("an explicit installed model name is required")
    if max_chars < 64 or target_chars < 1 or target_chars > max_chars:
        raise ValueError("invalid chunk size settings")
    output = _INDEX.safe_output(vault, output)
    old = _load_cache(output)
    settings = _settings(max_chars, target_chars)
    try:
        digest = model_digest(endpoint, model, timeout)
    except Exception:
        digest = None
    existing = old["pages"]
    pages = {}
    changed = []
    indexed_pages = set()
    reused_pages = set()
    source_text = {}
    for path in _page_list(vault):
        rel = path.relative_to(vault).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        source_text[rel] = text
        content_hash = sha256(text)
        prior = existing.get(rel)
        identity_ok = isinstance(prior, dict) and prior.get("model") == model and (digest is None or prior.get("modelDigest") == digest)
        if identity_ok and prior.get("contentHash") == content_hash and prior.get("settings") == settings:
            pages[rel] = prior
            reused_pages.add(rel)
        else:
            changed.append(rel)
    deferred = []
    for rel in changed:
        try:
            units = text_units(source_text[rel], max_chars)
            vectors = embeddings([unit["text"] for unit in units], endpoint, model, timeout)
            items = semantic_chunks(rel, source_text[rel], vectors, max_chars, target_chars)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            deferred.append(rel)
            continue
        pages[rel] = {"contentHash": sha256(source_text[rel]), "model": model,
                      "modelDigest": digest, "settings": settings, "chunks": items}
        indexed_pages.add(rel)
    # Recheck sources after service calls so edits during an embedding request cannot
    # publish stale vectors. Removed, retargeted, unreadable, or concurrently changed
    # pages are omitted instead of retaining old evidence.
    current_paths = {path.relative_to(vault).as_posix(): path for path in _page_list(vault)}
    for rel, entry in list(pages.items()):
        path = current_paths.get(rel)
        try:
            if path is None or not _INDEX.eligible(path, vault):
                raise OSError("source page is no longer eligible")
            current_hash = sha256(path.read_text(encoding="utf-8", errors="replace"))
            if current_hash != entry.get("contentHash"):
                raise OSError("source page changed during semantic build")
        except OSError:
            pages.pop(rel, None)
            deferred.append(rel)
    deferred = sorted(set(deferred))
    # Omitted pages are deleted; failed or racing changed pages are intentionally absent.
    result = {"version": CACHE_VERSION, "pages": pages}
    publish_cache(vault, result, output)
    surviving_pages = set(pages)
    return {"indexed": len(indexed_pages & surviving_pages), "reused": len(reused_pages & surviving_pages),
            "deferred": deferred, "modelDigest": digest, "cache": output}
