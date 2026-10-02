#!/usr/bin/env python3
"""Check PR CI and automatic main-release event, permission, and commit wiring."""

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
checks = workflows['test']
release = workflows['release']

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
assert checks['on']['push'] == {'branches': ['main']}
assert set(checks['on']['pull_request']['types']) == {'opened', 'synchronize', 'reopened', 'edited'}
assert 'merge_group' in checks['on']
assert 'workflow_call' not in checks['on']
assert checks['concurrency']['cancel-in-progress'] == 'true'
assert 'github.event.pull_request.number || github.ref' in checks['concurrency']['group']
assert set(checks['jobs']) == {'pr-title', 'test', 'ci'}
assert checks['defaults']['run']['shell'] == 'bash'

pr_title = checks['jobs']['pr-title']
assert pr_title['if'] == "github.event_name == 'pull_request'"
title_step = pr_title['steps'][0]
assert title_step['env'] == {'PR_TITLE': '${{ github.event.pull_request.title }}'}
for title, succeeds in (
    ('feat: add retrieval', True), ('fix(wiki): correct paths', True),
    ('feat!: change format', True), ('ci: update actions', True),
    ('unstructured title', False), ('fix: ', False), ('$(touch injected)', False),
):
    result = subprocess.run(['bash', '-eo', 'pipefail', '-c', title_step['run']],
                            env={**os.environ, 'PR_TITLE': title}, capture_output=True, text=True)
    assert (result.returncode == 0) == succeeds

job = checks['jobs']['test']
assert job['strategy']['fail-fast'] == 'false'
assert job['strategy']['matrix'] == {'os': ['ubuntu-latest', 'macos-latest']}
assert job['steps'][0]['with']['ref'] == '${{ github.sha }}'
node = next(step for step in job['steps'] if step.get('uses', '').startswith('actions/setup-node@'))
python = next(step for step in job['steps'] if step.get('uses', '').startswith('actions/setup-python@'))
assert node['with']['node-version'] == '24'
assert python['with']['python-version'] == '3.14'
commands = '\n'.join(step.get('run', '') for step in job['steps'])
for command in ('npm run release:check', 'npm test', 'npm run test:release', 'npm run test:package',
                'claude plugin validate .', 'bash tests/install-smoke.sh',
                'shellcheck tests/install-smoke.sh', 'sha256sum --check --ignore-missing',
                '"$work/actionlint" -color'):
    assert command in commands
assert 'NPM_TOKEN' not in commands

gate = checks['jobs']['ci']
assert gate['name'] == 'CI passed'
assert gate['if'] == 'always()'
assert gate['needs'] == ['pr-title', 'test']
for event in ('push', 'pull_request'):
    for title_result in ('success', 'failure', 'skipped', 'cancelled'):
        for test_result in ('success', 'failure', 'skipped', 'cancelled'):
            result = subprocess.run(['bash', '-eo', 'pipefail', '-c', gate['steps'][0]['run']],
                                    env={**os.environ, 'EVENT_NAME': event,
                                         'TITLE_RESULT': title_result, 'TEST_RESULT': test_result})
            expected_title = 'success' if event == 'pull_request' else 'skipped'
            assert (result.returncode == 0) == (test_result == 'success' and title_result == expected_title)

assert release['on'] == {
    'workflow_run': {'workflows': ['CI'], 'types': ['completed'], 'branches': ['main']},
}
assert release['concurrency'] == {
    'group': 'release-${{ github.repository }}', 'cancel-in-progress': 'false',
}
assert set(release['jobs']) == {'release'}
job = release['jobs']['release']
for condition in (
    "github.repository == 'soulwaxx/obsidian-second-brain'",
    "github.event.workflow_run.conclusion == 'success'",
    "github.event.workflow_run.event == 'push'",
    'github.event.workflow_run.head_repository.full_name == github.repository',
):
    assert condition in job['if']
assert job['permissions'] == {'contents': 'write', 'id-token': 'write'}
assert 'environment' not in job
assert job['steps'][0]['with']['ref'] == '${{ github.event.workflow_run.head_sha }}'
assert job['steps'][0]['with']['fetch-depth'] == '0'
semantic = job['steps'][-1]
assert semantic['uses'].startswith('cycjimmy/semantic-release-action@')
assert semantic['env'] == {
    'GITHUB_TOKEN': '${{ github.token }}', 'NPM_TOKEN': '${{ secrets.NPM_TOKEN }}',
    'NPM_CONFIG_PROVENANCE': 'true',
}
assert re.fullmatch(r'\d+\.\d+\.\d+', semantic['with']['semantic_version'])
assert semantic['with']['extra_plugins'] == 'conventional-changelog-conventionalcommits@${{ env.PRESET_VERSION }}'
config = json.loads((root / '.releaserc.json').read_text())
assert config['branches'] == ['main']
assert config['plugins'][0] == ['@semantic-release/commit-analyzer', {'preset': 'conventionalcommits', 'releaseRules': [
    {'breaking': True, 'release': 'major'}, {'type': 'feat', 'release': 'minor'},
    {'type': '*', 'release': 'patch'},
]}]
assert config['plugins'][1:3] == [
    ['@semantic-release/release-notes-generator', {'preset': 'conventionalcommits'}], '@semantic-release/npm',
]
assert config['plugins'][3] == ['@semantic-release/github', {
    'successComment': False, 'failComment': False, 'releasedLabels': False,
}]
manifest = json.loads((root / 'package.json').read_text())
assert manifest['scripts']['version'] == 'node scripts/release.mjs sync'
assert manifest['scripts']['prepublishOnly'] == 'npm run release:check'
assert 'release:verify' not in manifest['scripts']
assert 'release:publish' not in manifest['scripts']

renovate = workflows['renovate']
assert set(renovate['on']) == {'schedule', 'workflow_dispatch'}
action = renovate['jobs']['renovate']['steps'][-1]
assert action['with']['token'] == '${{ secrets.RENOVATE_TOKEN }}'
assert action['env']['RENOVATE_REPOSITORIES'] == '${{ github.repository }}'
config = json.loads((root / '.github/renovate.json').read_text())
assert 'helpers:pinGitHubActionDigests' in config['extends']
assert config['automerge'] is False
assert {'matchPackageNames': ['conventional-changelog-conventionalcommits'], 'allowedVersions': '<10'} in config['packageRules']
pattern = re.compile(config['customManagers'][0]['matchStrings'][0].replace('(?<', '(?P<'))
dependencies = {}
for file in (root / '.github/workflows').glob('*.yml'):
    for match in pattern.finditer(file.read_text()):
        dependencies[match['depName']] = (match['datasource'], match['currentValue'])
assert set(dependencies) == {
    'rhysd/actionlint', '@earendil-works/pi-coding-agent', '@anthropic-ai/claude-code',
    'npm', 'semantic-release', 'conventional-changelog-conventionalcommits', 'ghcr.io/renovatebot/renovate',
}
for datasource, version in dependencies.values():
    assert datasource in ('npm', 'github-releases', 'docker')
    assert re.fullmatch(r'v?\d+\.\d+\.\d+', version), version
print('PR CI and automatic release workflow guards PASS')
