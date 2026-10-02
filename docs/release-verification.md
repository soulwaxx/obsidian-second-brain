# Release verification

A [macOS and Ubuntu CI run](https://github.com/soulwaxx/obsidian-second-brain/actions/runs/36339968047) passed after the Bash 3 fix. A public release still requires passing CI for the release commit and all four authenticated client checks below. Authenticated first runs have not been recorded. Claude Code work-host live verification is tracked in [issue #1](https://github.com/soulwaxx/obsidian-second-brain/issues/1); package validation and install smoke do not count as live-client checks.

## Automated checks

`.github/workflows/test.yml` defines CI on main pushes, pull requests, merge groups, manual dispatch, and calls from the release workflow. A release call tests the exact tagged commit. Ubuntu and macOS each test Node 22/Python 3.12 and Node 24/Python 3.14. Direct tool versions and action SHAs are pinned.

1. Run actionlint on all workflows, check shell syntax, and run ShellCheck on `tests/install-smoke.sh` in the quality job.
2. Run `npm run release:check` to verify agreement among all three manifests in every runtime job.
3. Run `npm test` for lifecycle, middleware, bootstrap, retrieval, Pi extension, and packaged-agent metadata tests in every runtime job.
4. Run `npm run test:release` to check version synchronization, tags, publication refusal/retry behavior, npm latest protection, GitHub API failure handling, and workflow guards in every runtime job.
5. Run `npm run test:package` to inspect the npm archive, install it in a temporary directory, and test its packed Pi extension and middleware in every runtime job.
6. Install the pinned Pi and Claude Code CLIs, then run `claude plugin validate .` and `bash tests/install-smoke.sh` on the Node 22/Python 3.12 pair for each operating system.
7. Require every quality and matrix job to succeed through **CI passed**.

The install smoke sets temporary `HOME`, `PI_CODING_AGENT_DIR`, and `CLAUDE_CONFIG_DIR` paths. It installs the local checkout in each agent, checks installed resources, and confirms that a direct hook invocation outside a temporary vault leaves it unchanged. It does not launch an authenticated client session or send a model prompt.

Run the same package checks from the repository checkout:

```sh
npm run release:check
npm test
npm run test:release
npm run test:package
claude plugin validate .
bash tests/install-smoke.sh
```

Record the workflow URL, run ID, commit, operating system, and result of every check. A skipped or failed job does not count as a pass. The local smoke checks installation and hook behavior; it cannot prove that a live client loads the skill or runs its lifecycle events.

## Manual live-client checks

Use a disposable account or profile and a disposable Obsidian vault for each operating system and client. Obtain explicit consent before authenticating a client or sending a test prompt. Never use private notes for the protected-path test.

| Operating system | Client | Result | Date, versions, commit, and observations |
| --- | --- | --- | --- |
| macOS | Claude Code | UNTESTED | |
| macOS | Pi | UNTESTED | |
| Linux | Claude Code | UNTESTED | |
| Linux | Pi | UNTESTED | |

For each row, complete the following steps in the actual client:

1. Install this checkout through the client's local package path. Confirm that the client lists the wiki skill and loads the plugin or extension.
2. [Bootstrap](setup.md#2-preview-bootstrap) a disposable vault. Review its plan, apply the matching hash, and check that unrelated files remain unchanged.
3. Start the authenticated client from inside the vault. Invoke `/obsidian-second-brain:wiki` in Claude Code or `/skill:wiki` in Pi. Confirm the skill loads and the session-start index appears when `features.toc` is on. Then request a read-only summary from `@agent-obsidian-second-brain:wiki-vault` in Claude Code. For Pi, install the optional `pi-subagents` runner into the disposable profile, restart Pi, and run `/run obsidian-second-brain.wiki-vault "summarize wiki/quickstart.md"`. Confirm each specialist loads its packaged skill and reads the disposable vault, not the package checkout.
4. Ask the client to attempt a harmless edit to a protected generated path in the disposable vault. Confirm that the pre-write guard denies the edit and leaves the path unchanged. Check the expected session-end index behavior.
5. Start the same client from a directory outside the configured vault. Confirm that session start and shutdown leave the vault unchanged.
6. Record `PASS`, `FAIL`, or `UNTESTED`, along with the client version, operating-system version, commit, consent, and observed behavior. Keep `UNTESTED` if authentication, skill invocation, or any check above was skipped.

Directly running the hook is useful for diagnosis. It does not satisfy an actual-client check. Keep a failed result until you repeat the check on a fixed commit and record that run.

## npm distribution

[Releases and npm publishing](publishing.md) documents coordinated versioning, tags, the release pipeline, and one-time GitHub/npm setup. The release workflow checks these four rows for `PASS` with nonempty evidence before running macOS/Linux CI against the tag and entering the protected `release` environment. Approval must confirm that the evidence covers the code being released. Preparing or installing an npm archive does not satisfy the authenticated live-client gate. Complete the checks above for the release commit before publishing.

## Release decision

Check the GitHub Actions results for both operating systems and the four manual rows before claiming macOS/Linux host support. If a job or row is missing, record the release gate as pending. This repository does not claim Windows support; adding it requires separate checks.
