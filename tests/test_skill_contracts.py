#!/usr/bin/env python3
"""Keep focused wiki skills aligned with their shared contract."""

from pathlib import Path
import re
import subprocess
import sys
import tempfile

import yaml

ROOT = Path(__file__).resolve().parent.parent
SKILLS = {
    'wiki-query': ('read-only', 'search'),
    'wiki-save': ('selected', 'conversation'),
    'wiki-ingest': ('selected', 'source'),
    'wiki-research': ('bounded', 'evidence'),
    'wiki-health': ('read-only', 'doctor'),
}


def read_skill(name):
    path = ROOT / 'skills' / name / 'SKILL.md'
    text = path.read_text()
    assert text.startswith('---\n'), path
    frontmatter = yaml.safe_load(text.split('---\n', 2)[1])
    assert frontmatter['name'] == name
    assert frontmatter['description'].strip()
    for target in re.findall(r'\]\(([^)#]+\.md)(?:#[^)]+)?\)', text):
        assert (path.parent / target).resolve().is_file(), (path, target)
    return text


for skill, required in SKILLS.items():
    body = read_skill(skill).lower()
    assert 'focused workflows' in body
    assert 'wiki skill' in body
    for phrase in required:
        assert phrase in body, (skill, phrase)

save = (ROOT / 'skills/wiki-save/SKILL.md').read_text().lower()
assert 'do not save the transcript' in save
assert 'ambiguous' in save
assert 'search for an existing canonical page' in save

workflow_save = (ROOT / 'skills/wiki/references/focused-workflows.md').read_text().lower()
for boundary in ('only that selected material', 'make no changes', 'destination guard', 'validation, lifecycle finalization'):
    assert boundary in workflow_save, boundary

common = ' '.join((ROOT / 'skills/wiki/references/focused-workflows.md').read_text().lower().split())
for boundary in ('exact plan hash', 'wiki-ledger', 'does not provide web search', 'capture-inspect', 'capture-apply', 'batch-recover', 'source-identity', '.raw/agent-captures/', 'do not batch every small edit', 'search is read-only and reports fallback/freshness; it does not build an index', 'existing pages remain valid with only `type`', 'backfill/fabricate records for legacy pages', 'evidence-report --json', 'not that a claim is true or verified'):
    assert boundary in common

for workflow in ('wiki-research', 'wiki-ingest', 'wiki-save'):
    focused = (ROOT / 'skills' / workflow / 'SKILL.md').read_text().lower()
    assert 'optional' in focused
    assert 'evidence' in focused
assert 'leave freshness `unknown`' in common
save = (ROOT / 'skills/wiki-save/SKILL.md').read_text().lower()
assert 'conversation is not an independent external source' in save

transport = (ROOT / 'skills/wiki/references/focused-workflows.md').read_text()
for boundary in ('SKILL_DIR', 'SKILL_FILE', 'scripts/obsidian-second-brain.mjs', 'command -v', 'not a variable in the Bash environment'):
    assert boundary in transport, boundary
claude_fallback = transport.split('In Claude Code', 1)[1].split('```', 1)[1].split('```', 1)[0]
assert 'CLAUDE_PLUGIN_ROOT' not in claude_fallback
assert 'active skill directory' in transport
for name in ('wiki-query', 'wiki-health'):
    focused = (ROOT / 'skills' / name / 'SKILL.md').read_text().lower()
    assert 'package-relative launcher procedure' in focused, name
for document in ('README.md', 'docs/setup.md', 'skills/wiki/SKILL.md'):
    text = (ROOT / document).read_text()
    assert 'package-relative' in text.lower(), document

entry = (ROOT / 'skills/wiki/SKILL.md').read_text().lower()
assert 'general `/wiki` entry point' in entry
assert 'one agent that handles the whole lifecycle' not in entry
research_path = ROOT / 'skills/wiki/references/research-discipline.md'
frontmatter_path = ROOT / 'skills/wiki/references/frontmatter.md'
research = ' '.join(research_path.read_text().lower().split())
frontmatter = ' '.join(frontmatter_path.read_text().lower().split())
assert 'must carry a resolvable `source_url`' not in research
for boundary in ('canonical `resource`', 'legacy `source_url`', 'structured `sources`',
                 'local artifacts', 'session anchors', 'source-quality debt',
                 'not a validator failure', 'never fabricate',
                 'conversation is not an independent external source'):
    assert boundary in research, boundary
assert '(frontmatter.md#source-dates)' in research
assert '(research-discipline.md#provenance-discipline)' in frontmatter
for boundary in ('only required field', 'unrecognized fields', 'legacy scalar',
                 'string-list', 'producer-defined', '`retrieved` / `fetched`',
                 '`ingested`', '`observed`', '`published`', '`source_created`',
                 'not substitutes for retrieval dates', '`retrieval_status: unknown`',
                 '`retrieval_status: unavailable`'):
    assert boundary in frontmatter, boundary

source_examples = [yaml.safe_load(block) for block in
                   re.findall(r'^```yaml\n(.*?)\n```$', frontmatter_path.read_text(), re.S | re.M)
                   if block.startswith('type: source\n')]
assert len(source_examples) == 5, 'Keep external, bundle, local, session, and unknown examples'
external = next(item for item in source_examples if 'retrieved' in item)
assert external['resource'].startswith('https://')
bundle = next(item for item in source_examples if 'ingested' in item)
assert len(bundle['sources']) == 2
assert all(entry['id'] and entry['resource'] for entry in bundle['sources'])
assert any(item.get('resource', '').startswith('.raw/') for item in source_examples)
session = next(item for item in source_examples if 'observed' in item)
assert session['sources'][0]['id'] and 'resource' not in session['sources'][0]
unknown = next(item for item in source_examples
               if item.get('retrieval_status') == 'unknown'
               and item.get('resource', '').startswith('https://'))
assert not {'retrieved', 'fetched', 'ingested'} & unknown.keys()
assert all('source_url' not in item for item in source_examples)
with tempfile.TemporaryDirectory() as temp:
    validator = ROOT / 'skills/wiki/scripts/okf_mw/validate.py'
    for number, example in enumerate(source_examples):
        page = Path(temp) / f'source-{number}.md'
        page.write_text('---\n' + yaml.safe_dump(example) + '---\nSynthetic source.\n')
        result = subprocess.run([sys.executable, str(validator), str(page)],
                                text=True, capture_output=True)
        assert result.returncode == 0, (example, result.stderr)
print('Focused wiki skill contracts PASS')
