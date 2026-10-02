#!/usr/bin/env python3
"""Check CI/release event, permission, commit, and verification wiring."""

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
release = workflows['release']
checks = workflows['test']
renovate = workflows['renovate']

for workflow in workflows.values():
    assert workflow['permissions'] == {'contents': 'read'}
    assert 'pull_request_target' not in workflow['on']
    for job in workflow['jobs'].values():
        if 'uses' in job:
            continue
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

assert release['on']['push']['tags'] == ['v*']
assert release['on']['workflow_dispatch']['inputs']['tag']['required'] == 'true'
assert release['concurrency'] == {
    'group': 'release-${{ github.repository }}', 'cancel-in-progress': 'false',
}
validate = release['jobs']['validate']
assert validate['if'] == "github.repository == 'soulwaxx/obsidian-second-brain'"
checkout = validate['steps'][0]['with']
assert "format('refs/tags/{0}', inputs.tag)" in checkout['ref']
assert checkout['fetch-depth'] == '0'
validation = next(step for step in validate['steps'] if step.get('id') == 'release')
assert validation['env']['WORKFLOW_REF'] == '${{ github.ref }}'
assert validation['env']['WORKFLOW_SHA'] == '${{ github.sha }}'
assert '[ "$WORKFLOW_REF" != "refs/tags/$RELEASE_TAG" ]' in validation['run']
assert 'test "$(git rev-parse HEAD)" = "$WORKFLOW_SHA"' in validation['run']
assert 'node scripts/release.mjs check "$RELEASE_TAG"' in validation['run']
assert 'node scripts/release.mjs verify-live' in validation['run']
assert 'git rev-parse HEAD' in validation['run']
provenance_guard = validation['run'].split('node scripts/release.mjs check')[0]
sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
for workflow_ref, workflow_sha, succeeds in (
    ('refs/tags/v0.1.0', sha, True),
    ('refs/heads/main', sha, False),
    ('refs/tags/v0.1.1', sha, False),
    ('refs/tags/v0.1.0', '0' * 40, False),
):
    result = subprocess.run(['bash', '-eo', 'pipefail', '-c', provenance_guard], cwd=root,
                            env={**os.environ, 'RELEASE_TAG': 'v0.1.0',
                                 'WORKFLOW_REF': workflow_ref, 'WORKFLOW_SHA': workflow_sha},
                            capture_output=True, text=True)
    assert (result.returncode == 0) == succeeds

test = release['jobs']['test']
assert test['needs'] == 'validate'
assert test['uses'] == './.github/workflows/test.yml'
assert test['with']['ref'] == '${{ needs.validate.outputs.sha }}'
assert 'workflow_call' in checks['on']
assert checks['on']['push'] == {'branches': ['main']}
assert 'pull_request' in checks['on'] and 'merge_group' in checks['on']
assert 'github.event.pull_request.number || github.ref' in checks['concurrency']['group']
assert "inputs.ref || 'branch'" in checks['concurrency']['group']
assert checks['concurrency']['cancel-in-progress'] == "${{ inputs.ref == '' }}"
assert checks['defaults']['run']['shell'] == 'bash'
assert release['defaults']['run']['shell'] == 'bash'
matrix_job = checks['jobs']['test']
assert matrix_job['name'] == '${{ matrix.runtime.name }} (${{ matrix.os }})'
assert matrix_job['strategy']['fail-fast'] == 'false'
assert set(matrix_job['strategy']['matrix']['os']) == {'ubuntu-latest', 'macos-latest'}
assert matrix_job['strategy']['matrix']['runtime'] == [
    {'name': 'test', 'node': '22', 'python': '3.12'},
    {'name': 'compatibility', 'node': '24', 'python': '3.14'},
]
for job in (matrix_job, checks['jobs']['quality']):
    assert job['steps'][0]['with']['ref'] == '${{ inputs.ref || github.sha }}'
commands = '\n'.join(step.get('run', '') for step in matrix_job['steps'])
for command in ('npm run release:check', 'npm test', 'npm run test:release', 'npm run test:package',
                'claude plugin validate .', 'bash tests/install-smoke.sh'):
    assert command in commands
for name in ('Install agent CLIs', 'Validate Claude plugin and marketplace',
             'Isolated local package installation smoke'):
    step = next(step for step in matrix_job['steps'] if step.get('name') == name)
    assert step['if'] == "matrix.runtime.name == 'test'"
quality = '\n'.join(step.get('run', '') for step in checks['jobs']['quality']['steps'])
assert 'sha256sum --check --ignore-missing' in quality
assert '"$work/actionlint" -color' in quality
assert 'shellcheck tests/install-smoke.sh' in quality

gate = checks['jobs']['ci']
assert gate['name'] == 'CI passed'
assert gate['if'] == 'always()'
assert gate['needs'] == ['quality', 'test']
assert gate['steps'][0]['env'] == {
    'QUALITY_RESULT': '${{ needs.quality.result }}', 'TEST_RESULT': '${{ needs.test.result }}',
}
for quality_result in ('success', 'failure', 'skipped', 'cancelled'):
    for test_result in ('success', 'failure', 'skipped', 'cancelled'):
        result = subprocess.run(['bash', '-eo', 'pipefail', '-c', gate['steps'][0]['run']],
                                env={**os.environ, 'QUALITY_RESULT': quality_result, 'TEST_RESULT': test_result})
        assert (result.returncode == 0) == (quality_result == test_result == 'success')

publish = release['jobs']['publish']
assert publish['needs'] == ['validate', 'test']
assert publish['environment'] == 'release'
assert publish['permissions'] == {'contents': 'write', 'id-token': 'write'}
assert publish['steps'][0]['with']['ref'] == '${{ needs.validate.outputs.sha }}'
assert publish['steps'][0]['with']['fetch-depth'] == '0'
node = next(step for step in publish['steps'] if step.get('uses', '').startswith('actions/setup-node@'))
assert node['with']['node-version'] == '24'
assert node['with']['registry-url'] == 'https://registry.npmjs.org'
npm = next(step for step in publish['steps'] if step.get('name') == 'Publish npm package')
assert npm['run'] == 'npm run release:publish -- "$RELEASE_TAG"'
assert npm['env']['NODE_AUTH_TOKEN'] == '${{ secrets.NPM_TOKEN }}'
assert npm['env']['RELEASE_TAG'] == '${{ needs.validate.outputs.tag }}'
remote_tag = next(step for step in publish['steps'] if step.get('name') == 'Recheck the remote tag after approval')
assert publish['steps'].index(remote_tag) < publish['steps'].index(npm)
assert 'git fetch --no-tags origin "refs/tags/$RELEASE_TAG"' in remote_tag['run']
assert "git rev-parse 'FETCH_HEAD^{commit}'" in remote_tag['run']
assert 'git rev-parse HEAD' in remote_tag['run']
github_release = publish['steps'][-1]
assert github_release['env']['GH_TOKEN'] == '${{ github.token }}'
assert github_release['run'] == 'node scripts/release.mjs github-release "$RELEASE_TAG"'
assert github_release['env']['RELEASE_TAG'] == '${{ needs.validate.outputs.tag }}'

assert set(renovate['on']) == {'schedule', 'workflow_dispatch'}
assert renovate['on']['schedule'] == [{'cron': '0 3 * * 6'}]
assert renovate['concurrency']['cancel-in-progress'] == 'false'
job = renovate['jobs']['renovate']
assert job['if'] == "github.repository == 'soulwaxx/obsidian-second-brain' && github.ref == 'refs/heads/main'"
action = job['steps'][-1]
assert action['with']['token'] == '${{ secrets.RENOVATE_TOKEN }}'
assert action['with']['configurationFile'] == '.github/renovate.json'
assert action['env']['RENOVATE_REPOSITORIES'] == '${{ github.repository }}'
config = json.loads((root / '.github/renovate.json').read_text())
assert 'helpers:pinGitHubActionDigests' in config['extends']
assert config['automerge'] is False
assert set(config['enabledManagers']) == {'github-actions', 'npm', 'pip_requirements', 'custom.regex'}
pattern = re.compile(config['customManagers'][0]['matchStrings'][0].replace('(?<', '(?P<'))
dependencies = {}
for file in (root / '.github/workflows').glob('*.yml'):
    for match in pattern.finditer(file.read_text()):
        dependencies[match['depName']] = (match['datasource'], match['currentValue'])
assert set(dependencies) == {
    'rhysd/actionlint', '@earendil-works/pi-coding-agent', '@anthropic-ai/claude-code',
    'npm', 'ghcr.io/renovatebot/renovate',
}
for datasource, version in dependencies.values():
    assert datasource in ('npm', 'github-releases', 'docker')
    assert re.fullmatch(r'v?\d+\.\d+\.\d+', version), version
assert re.fullmatch(r'PyYAML==\d+\.\d+\.\d+\n', (root / '.github/requirements-ci.txt').read_text())
print('CI and release workflow guards PASS')
