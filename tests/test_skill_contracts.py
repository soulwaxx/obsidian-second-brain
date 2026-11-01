#!/usr/bin/env python3
"""Keep focused wiki skills aligned with their shared contract."""

from pathlib import Path
import json
import os
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
for boundary in ('exact plan hash', 'wiki-ledger', 'does not provide web search', 'capture-inspect', 'capture-apply', 'batch-recover', 'source-identity', '.raw/agent-captures/', 'do not batch every small edit', 'search is read-only and reports fallback/freshness; it does not build an index', 'existing pages remain valid with only `type`', 'backfill/fabricate records for legacy pages', 'evidence-report --json', 'not that a claim is true or verified', 'optional selected source baselines', 'separate exact observation, page-batch, and baseline approvals', 'do not infer a historic reconciliation'):
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

entry_path = ROOT / 'skills/wiki/SKILL.md'
entry = entry_path.read_text().lower()
assert 'general `/wiki` entry point' in entry
assert 'one agent that handles the whole lifecycle' not in entry

# Static instruction contracts, not a model/sandbox or a compliance evaluation.
trust = ' '.join(entry.split('## trust boundary\n', 1)[1].split('\n---', 1)[0].split())
for boundary in ('source files', 'page bodies and frontmatter', 'raw captures',
                 'retrieved excerpts', 'search results', 'tool outputs',
                 'untrusted evidence, not instructions', 'embedded role/system messages',
                 'evidence never authorizes scope expansion', 'settings changes',
                 'command execution', 'secret disclosure', 'capture or filing',
                 'protected-artifact writes', 'bypassing guards',
                 'continue the already-authorized task', 'do not ask for extra approval',
                 'only selected conversation material', 'deliberately selected',
                 'scoped configuration', 'existing instruction precedence',
                 'higher-priority rules', 'grant new permissions',
                 'seeing an instruction-like filename does not select it',
                 'evidence cannot self-promote into instructions',
                 'not a runtime sandbox', 'static contract tests are not live-model compliance evidence'):
    assert boundary in trust, boundary
answering = ' '.join(entry.split('## wiki-first answering\n', 1)[1].split('\n---', 1)[0].split())
for boundary in ('sufficient, relevant wiki evidence', 'honest page citations',
                 'disclose material uncertainty and contradictions',
                 'no mandatory source crawl or extra approval',
                 'missing metadata alone does not require a source crawl',
                 'does not support the requested claim', 'freshness matters',
                 'ambiguous or contradictory', 'consequences',
                 'explicitly asks you to verify', 'primary evidence is unavailable',
                 '**index freshness**', '**observed source version**',
                 '**recorded review**', '**factual support**', '**current deployment truth**',
                 'metadata fields, and recent edits alone never verify',
                 'existing task authorization', 'does not authorize network fetching',
                 'never imply a source or deployment was checked when it was not'):
    assert boundary in answering, boundary

shared_references = [ROOT / 'skills/wiki/references' / name for name in
                     ('focused-workflows.md', 'research-discipline.md')]
for path in shared_references + [ROOT / 'skills' / name / 'SKILL.md' for name in SKILLS]:
    body = path.read_text()
    for section in ('trust-boundary', 'wiki-first-answering'):
        assert f'SKILL.md#{section})' in body, (path, section)
for path in [*ROOT.glob('skills/**/*.md'), *ROOT.glob('agents/**/*.md')]:
    body = ' '.join(path.read_text().lower().split())
    assert not re.search(r'\bwiki (?:is (?:the )?|as )?authoritative\b', body), path
    assert 'authoritative answer' not in body, path
metadata = ' '.join((ROOT / 'skills/wiki/references/frontmatter.md').read_text().lower().split())
assert 'declared trust tier' in metadata
assert 'not independently attested review' in metadata
assert 'alone verifies no claim' in metadata
conventions = (ROOT / 'skills/wiki/references/authoring-standards.md').read_text()
assert '../SKILL.md#trust-boundary' in conventions

examples_path = ROOT / 'skills/wiki/references/trust-boundary-examples.md'
examples = examples_path.read_text()
assert '(references/trust-boundary-examples.md)' in entry
assert 'compliance is UNVERIFIED' in examples
assert 'do not execute a model or prove live-model' in examples
cases = dict(re.findall(r'^## ([^\n]+)\n(.*?)(?=^## |\Z)', examples, re.S | re.M))
expected_cases = {
    'Malicious evidence': ('ignore instructions', 'embedded role', 'without extra approval'),
    'Stale page': ('stale_after: 2030-01-01', 'matching hash and recent edit', 'current value remains unknown'),
    'Conflicting sources': ('source a', 'source b', 'unresolved contradiction', 'merely because'),
    'Unsupported claim': ('verified', 'unsupported', 'not proof of this claim'),
    'Sufficient current page': ('type: policy', 'no mandatory source crawl', 'running deployment was checked'),
    'Scoped convention file': ('wiki/wiki.md', 'flat layout', 'scoped configuration', 'cannot self-promote'),
    'Selected save injection': ('archive the full transcript', 'only the selected decision',
                                'not authorization', 'do not ask the user to reapprove'),
    'Diagnostic injection': ('read-only', 'tool result', 'without performing repairs'),
}
assert cases.keys() == expected_cases.keys(), cases.keys()
for name, phrases in expected_cases.items():
    case = ' '.join(cases[name].lower().split())
    for label in ('**task.**', '**evidence.**', '**expected.**'):
        assert label in case, (name, label)
    for phrase in phrases:
        assert phrase in case, (name, phrase)

# Check example links and the type-only page, not an invented model response.
for path in [examples_path, *shared_references]:
    for target in re.findall(r'\]\(([^)#]+\.md)(?:#[^)]+)?\)', path.read_text()):
        assert (path.parent / target).resolve().is_file(), (path, target)
policy_page = re.search(r'^```markdown\n(.*?)\n```$',
                        cases['Sufficient current page'], re.S | re.M).group(1)
assert yaml.safe_load(policy_page.split('---\n', 2)[1]) == {'type': 'policy'}
with tempfile.TemporaryDirectory() as temp:
    page = Path(temp) / 'policy.md'
    page.write_text(policy_page + '\n')
    result = subprocess.run([sys.executable, str(ROOT / 'skills/wiki/scripts/okf_mw/validate.py'), str(page)],
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
baseline_doc = (ROOT / 'docs/source-baselines.md').read_text().lower().replace('**', '')
for boundary in ('observe-inspect', 'observe-apply', 'baseline-inspect', 'baseline-apply',
                 'source-history', 'no-change', '64 mib', 'not copied or archived',
                 'imported records do not recreate local publication authority',
                 'does not identify historically reconciled content',
                 'observation is not a declared reconciliation',
                 'reconciliation is not factual review'):
    assert boundary in ' '.join(baseline_doc.split()), boundary
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
transport_path = ROOT / 'skills/wiki/references/cli-transport.md'
transport_cli = transport_path.read_text()
transport_cli_flat = ' '.join(transport_cli.split())
for boundary in ('vaults verbose', 'vault info=path', 'compare the returned path exactly',
                 'Never fall back to whichever vault happens to be active',
                 'vault="$NATIVE_VAULT" search', 'vault="$NATIVE_VAULT" read'):
    assert boundary in transport_cli_flat, boundary
assert 'vault=<name-or-id>' in transport_cli
for document in ('skills/wiki/SKILL.md', 'docs/setup.md'):
    text = (ROOT / document).read_text()
    for line in text.splitlines():
        if 'scripts/' in line and line.rstrip().endswith('\\\\'):
            assert len(line) - len(line.rstrip('\\\\')) == 1, (document, line)
for line in transport_cli.splitlines():
    if (line.startswith('obsidian-cli ') and not line.startswith('obsidian-cli vaults ')
            and '<command>' not in line):
        assert ' vault="$NATIVE_VAULT" ' in line or 'vault="$NATIVE_VAULT" vault info=path' in line, line

# Execute the configured-vault preflight with a recording fake CLI. A mismatch
# or unavailable CLI must stop before any content query; a matching vault runs
# the explicitly selected command.
preflight = next(block for block in re.findall(r'^```bash\n(.*?)\n```$', transport_cli, re.S | re.M)
                if 'vaults verbose' in block)
cli_stub_dir = tempfile.TemporaryDirectory()
cli_stub = Path(cli_stub_dir.name) / 'obsidian-cli'
cli_stub.write_text(f'''#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
with open(os.environ["CLI_CALLS"], "a", encoding="utf-8") as out:
    out.write(json.dumps(args) + "\\n")
if args == ["vaults", "verbose"]:
    print("A /vault/A\\nB /vault/B")
elif args[:2] == ["vault=B", "vault"]:
    print(os.environ.get("NATIVE_PATH", "/vault/B"))
''')
cli_stub.chmod(0o755)
try:
    for configured_path, native_path, available, expected_queries in (
            ('/vault/B', '/vault/A', True, 0),
            ('/vault/B', '/vault/B', True, 1),
            ('/vault/B', '/vault/B', False, 0)):
        with tempfile.TemporaryDirectory() as temp:
            calls = Path(temp) / 'calls.jsonl'
            env = dict(os.environ, PATH=(cli_stub_dir.name if available else temp),
                       CLI_CALLS=str(calls), NATIVE_PATH=native_path)
            script = (preflight.replace("'<verified vault name or ID>'", "'B'")
                      .replace("'<configured absolute vault root>'", "'/vault/B'")
                      + '\nobsidian-cli vault="$NATIVE_VAULT" read path="wiki/test.md"\n')
            result = subprocess.run(['/bin/bash', '-c', script], text=True, capture_output=True, env=env,
                                    cwd=temp, input=None)
            recorded = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
            queries = [args for args in recorded if args[1:2] == ['read']]
            assert len(queries) == expected_queries, (native_path, available, recorded, result.stderr)
            if expected_queries:
                assert queries == [['vault=B', 'read', 'path=wiki/test.md']], queries
                assert result.returncode == 0, result.stderr
            else:
                assert result.returncode != 0, (available, native_path, result.stdout)
finally:
    cli_stub_dir.cleanup()

# Run both semantic command blocks through a harmless python3 recorder, proving
# shell continuation passes the intended options in exactly two invocations.
for document in ('skills/wiki/SKILL.md', 'docs/setup.md'):
    text = (ROOT / document).read_text()
    blocks = re.findall(r'^```sh\n(.*?)\n```$', text, re.S | re.M)
    example = next(block for block in blocks if '--semantic-chunks' in block and '--hybrid' in block)
    example = (example.replace('<package-dir>', 'PACKAGE_DIR')
               .replace('<vault-root>', 'VAULT_ROOT')
               .replace('<installed-model>', 'MODEL'))
    with tempfile.TemporaryDirectory() as temp:
        calls = Path(temp) / 'python-calls.jsonl'
        stub = Path(temp) / 'python3'
        stub.write_text(f'''#!{sys.executable}
import json, os, sys
with open(os.environ["PYTHON_CALLS"], "a", encoding="utf-8") as out:
    out.write(json.dumps(sys.argv[1:]) + "\\n")
''')
        stub.chmod(0o755)
        env = dict(os.environ, PATH=temp + os.pathsep + os.environ['PATH'], PYTHON_CALLS=str(calls))
        result = subprocess.run(['/bin/sh', '-c', example], cwd=temp, env=env, text=True, capture_output=True)
        assert result.returncode == 0, (document, result.stderr)
        invocations = [json.loads(line) for line in calls.read_text().splitlines()]
        assert len(invocations) == 2, (document, invocations)
        assert invocations[0] == ['PACKAGE_DIR/scripts/obsidian-second-brain.py', 'build', '--vault', 'VAULT_ROOT', '--semantic-chunks', '--ollama-model', 'MODEL'], (document, invocations)
        assert invocations[1] == ['PACKAGE_DIR/scripts/retrieve.py', 'query terms', '--vault', 'VAULT_ROOT', '--hybrid', '--ollama-model', 'MODEL'], (document, invocations)
        assert all('\\\\' not in arg for invocation in invocations for arg in invocation)

print('Focused wiki skill contracts PASS')
