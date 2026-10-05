#!/usr/bin/env python3
"""Check pre-merge CI, trusted-source publishing, metadata, and tool maintenance."""

import json
import os
import re
import subprocess
from pathlib import Path

import yaml

root = Path(__file__).resolve().parent.parent
workflows = {
    file.stem: yaml.load(file.read_text(), Loader=yaml.BaseLoader)
    for file in (root / '.github/workflows').glob('*.yml')
}
checks, release, metadata = (workflows[name] for name in ('test', 'release', 'pr-title'))
for workflow in workflows.values():
    assert workflow['permissions'] == {'contents': 'read'}
    assert 'pull_request_target' not in workflow['on']
    for job in workflow['jobs'].values():
        assert 0 < int(job['timeout-minutes']) <= 30
        assert 'continue-on-error' not in job
        for step in job['steps']:
            assert 'continue-on-error' not in step
            if 'uses' in step:
                assert re.fullmatch(r'[\w-]+/[\w-]+@[0-9a-f]{40}', step['uses']), step['uses']
            if step.get('uses', '').startswith('actions/checkout@'):
                assert step['with']['persist-credentials'] == 'false'
            if step.get('uses', '').startswith('actions/setup-node@'):
                assert step['with']['package-manager-cache'] == 'false'

assert checks['name'] == 'CI'
assert 'push' not in checks['on']
assert set(checks['on']['pull_request']['types']) == {'opened', 'synchronize', 'reopened'}
assert {'merge_group', 'workflow_dispatch'} <= checks['on'].keys()
assert checks['concurrency']['cancel-in-progress'] == 'true'
assert set(checks['jobs']) == {'test', 'ci'}
job = checks['jobs']['test']
assert job['strategy']['matrix'] == {'os': ['ubuntu-latest', 'macos-latest']}
assert job['steps'][0]['with']['ref'] == '${{ github.sha }}'
commands = '\n'.join(step.get('run', '') for step in job['steps'])
for command in ('npm run release:check', 'npm test', 'npm run test:release', 'npm run test:package',
                'claude plugin validate .', 'bash tests/install-smoke.sh',
                'shellcheck tests/install-smoke.sh', 'sha256sum --check --ignore-missing'):
    assert command in commands

# Metadata edits never restart the expensive matrix or cancel its evidence run.
assert set(metadata['on']['pull_request']['types']) == {'opened', 'synchronize', 'reopened', 'edited'}
assert metadata['concurrency']['group'] != checks['concurrency']['group']
title = metadata['jobs']['title']
assert title['name'] == 'PR title passed'
assert len(title['steps']) == 1
step = title['steps'][0]
assert step['env'] == {'PR_TITLE': '${{ github.event.pull_request.title }}'}
for value, succeeds in (
    ('feat: add retrieval', True), ('fix(wiki): correct paths', True),
    ('feat!: change format', True), ('ci: update actions', True),
    ('unstructured title', False), ('fix: ', False), ('$(touch injected)', False),
):
    result = subprocess.run(['bash', '-eo', 'pipefail', '-c', step['run']],
                            env={**os.environ, 'PR_TITLE': value}, capture_output=True, text=True)
    assert (result.returncode == 0) == succeeds

gate = checks['jobs']['ci']
assert gate['name'] == 'CI passed'
assert gate['if'] == 'always()'
assert gate['needs'] == ['test']
for result in ('success', 'failure', 'skipped', 'cancelled'):
    checked = subprocess.run(['bash', '-eo', 'pipefail', '-c', gate['steps'][0]['run']],
                             env={**os.environ, 'TEST_RESULT': result})
    assert (checked.returncode == 0) == (result == 'success')
record = next(step for step in gate['steps'] if 'release-source.mjs record' in step.get('run', ''))
assert record['if'] == "github.event_name == 'pull_request'"
assert record['env'] == {
    'PR_NUMBER': '${{ github.event.pull_request.number }}',
    'PR_HEAD_SHA': '${{ github.event.pull_request.head.sha }}',
    'PR_BASE_SHA': '${{ github.event.pull_request.base.sha }}',
}
artifact = gate['steps'][-1]
assert artifact['uses'].startswith('actions/upload-artifact@')
assert artifact['with']['name'] == 'verified-source-${{ github.run_attempt }}'
assert artifact['with']['if-no-files-found'] == 'error'

assert release['on']['push'] == {'branches': ['main']}
assert 'workflow_run' not in release['on']
assert release['concurrency']['cancel-in-progress'] == 'false'
verify = release['jobs']['verify']
assert verify['if'] == "github.repository == 'soulwaxx/obsidian-second-brain' && github.ref == 'refs/heads/main'"
assert release['on']['workflow_dispatch']['inputs']['tag'] == {
    'description': 'Recover missing outputs for an existing stable vX.Y.Z tag',
    'required': 'true', 'type': 'string',
}
assert verify['permissions'] == {'contents': 'read', 'actions': 'read', 'pull-requests': 'read'}
assert 'release-source.mjs verify' in verify['steps'][-1]['run']
job = release['jobs']['release']
assert job['needs'] == ['verify']
assert job['permissions'] == {'contents': 'write', 'id-token': 'write'}
assert 'environment' not in job
assert job['steps'][0]['with']['ref'] == '${{ needs.verify.outputs.source_sha }}'
assert job['steps'][0]['with']['fetch-depth'] == '0'
python = next(step for step in job['steps'] if step.get('uses', '').startswith('actions/setup-python@'))
assert python['with']['python-version'] == '3.14'
prerequisites = next(step for step in job['steps'] if step.get('name') == 'Install artifact-check prerequisites')
assert 'apt-get install -y jq' in prerequisites['run']
assert 'pip install --requirement .github/requirements-ci.txt' in prerequisites['run']
semantic = next(step for step in job['steps'] if step.get('run') == '.github/release-tools/node_modules/.bin/semantic-release')
assert semantic['if'] == "github.event_name == 'push'"
assert semantic['env'] == {
    'GITHUB_TOKEN': '${{ github.token }}', 'GH_TOKEN': '${{ github.token }}', 'NPM_CONFIG_PROVENANCE': 'true',
}
recovery = job['steps'][-1]
assert recovery['if'] == "github.event_name == 'workflow_dispatch'"
assert recovery['env']['RECOVERY_TAG'] == '${{ inputs.tag }}'
assert 'release-recovery.mjs recover' in recovery['run']
assert 'NPM_TOKEN' not in json.dumps(release)
for steps in (checks['jobs']['test']['steps'], job['steps']):
    tool_commands = '\n'.join(step.get('run', '') for step in steps)
    assert 'npm ci --prefix .github/release-tools --ignore-scripts' in tool_commands
    assert 'node .github/release-tools/check.mjs' in tool_commands
    assert 'npm audit --prefix .github/release-tools --audit-level=critical' in tool_commands
config = json.loads((root / '.releaserc.json').read_text())
assert config['branches'] == ['main']
assert config['plugins'][0][1]['releaseRules'] == [
    {'breaking': True, 'release': 'major'}, {'type': 'feat', 'release': 'minor'}, {'type': '*', 'release': 'patch'},
]
assert config['plugins'][2:4] == ['@semantic-release/npm', './scripts/release-artifact.mjs']
assert config['plugins'][4] == ['@semantic-release/github', {
    'successCommentCondition': False, 'failCommentCondition': False, 'releasedLabels': False,
}]
tools = json.loads((root / '.github/release-tools/package.json').read_text())
lock = json.loads((root / '.github/release-tools/package-lock.json').read_text())
assert tools['private'] is True
assert lock['packages']['']['dependencies'] == tools['dependencies']
assert set(tools['dependencies']) == {'semantic-release', 'conventional-changelog-conventionalcommits'}
for name, version in tools['dependencies'].items():
    assert re.fullmatch(r'\d+\.\d+\.\d+', version)
    assert lock['packages']['node_modules/' + name]['version'] == version
for entry in lock['packages'].values():
    if 'resolved' in entry:
        assert entry['resolved'].startswith('https://registry.npmjs.org/')
        assert 'integrity' in entry
assert 'bash tests/install-smoke.sh' in commands
assert 'tests/install-smoke.sh' in (root / 'scripts/release-artifact.mjs').read_text()
manifest = json.loads((root / 'package.json').read_text())
assert manifest['scripts']['version'] == 'node scripts/release.mjs sync'
assert manifest['scripts']['prepublishOnly'] == 'npm run release:check'

renovate = workflows['renovate']
assert set(renovate['on']) == {'schedule', 'workflow_dispatch'}
action = renovate['jobs']['renovate']['steps'][-1]
assert action['with']['token'] == '${{ secrets.RENOVATE_TOKEN }}'
config = json.loads((root / '.github/renovate.json').read_text())
assert 'helpers:pinGitHubActionDigests' in config['extends']
assert config['automerge'] is False
pattern = re.compile(config['customManagers'][0]['matchStrings'][0].replace('(?<', '(?P<'))
dependencies = {}
for file in (root / '.github/workflows').glob('*.yml'):
    for match in pattern.finditer(file.read_text()):
        dependencies[match['depName']] = (match['datasource'], match['currentValue'])
assert {'rhysd/actionlint', '@earendil-works/pi-coding-agent', '@anthropic-ai/claude-code',
        'npm', 'ghcr.io/renovatebot/renovate'} <= dependencies.keys()
for datasource, version in dependencies.values():
    assert datasource in ('npm', 'github-releases', 'docker')
    assert re.fullmatch(r'v?\d+\.\d+\.\d+', version), version
print('Pre-merge CI and trusted-source release workflow guards PASS')
