# Tool-service authentication release packet

Date: 23 November 2026
Status: integrated verification green — **no tag, pin move, deployment, or notice activation**

## 1. Exact references

| Item | Reference |
| --- | --- |
| Current Collegium pin | `553a04e8ac0aa49fd1f61d05118a1e469b8eb6fb` |
| Authentication candidate | `7f0211c46ca9ab43d5f25cbbf572521fe2e23d59` |
| Current `exp/dreaming-pass` head | `fc18124f8cd2b4dc217bb53271d1cfe1eecad273` |
| Contract-test branch | `7be6063032637e9ff77e81e87190e6c9ad851188` |
| Integrated authentication commit | `089a07517d0352a3f322c4c110dfd8cca3f131fe` |
| SC-25 lab commit with the Git safe-directory fix | `8568f2a7` |

The authentication branch is `feat/tool-service-run-auth`. It is pushed. No pull request is open for it.

The current Collegium pin has not moved. The authentication candidate is not active for members.

## 2. Evidence already obtained

| Check | Result and basis |
| --- | --- |
| SC-28 first run, patched | 7 of 7 checks passed. The control completed 4 of 6 checks. |
| SC-28 two-run reuse, patched | 17 of 17 checks passed. The control completed 12 of 17 checks. |
| Token rotation | After run 2, run 1's bearer returns `401`; run 2's bearer returns `200`. Supervisor and child PIDs are reused. The token file is `0600`. |
| Record and configuration scan | No bearer values found in the reviewed records. |
| x86_64 SC-28 | 7 of 7 checks passed with OpenCode v2.3.0 amd64. |
| x86_64 live SC-25 on the exact authentication commit | 40 of 40 checks passed after one retry. An earlier second-run timeout did not recur. |
| Full offline release gate | 4,044 tests passed, 2 skipped, with the real Magnolia candidate. The contract suite then passed 35 tests. No provider credential was requested. |
| Authentication-focused suites | 13 authentication tests passed; the compchem-tools suite reported 352 passed, 3 skipped. |

Primary retained evidence:

- `/home/tjiang/download/sc28-records-2026-11-20.zip`
- Contract-test branch `test/magnolia-contract` at `7be6063032637e9ff77e81e87190e6c9ad851188`

The SC-28 archive confirms both directions of rotation: the old bearer fails and the new bearer succeeds, without changing the service owner PID or child PID.

## 3. Integrated verification completed on 24 November

The integrated commit `089a07517d0352a3f322c4c110dfd8cca3f131fe` combines the authentication candidate with `fc18124f`.

| Check | Result |
| --- | --- |
| Real-Magnolia contract suite | 35 passed. FastMCP 4.2.5 was used, with package imports confirmed from the integrated checkout. |
| Full offline release gate | 4,044 passed, 2 skipped, followed by another 35 contract tests. No provider credential was requested. |
| Live x86_64 SC-25 | 40 of 40 checks passed on PR 209 with the integrated Magnolia runtime. |

The live report is at:

```text
/tmp/opencode/collegium-pr209/.runtime/lab-runs/20261124T090437Z-SC-25/report.md
```

Two environment details matter for reproducing the result:

- A normal clone was required because Docker cannot use a worktree's `.git` pointer file without also mounting its referenced Git directory.
- The lab image needed the trial Git directory marked safe. PR 209 carries that fix as `8568f2a7`. The architecture-aware OpenCode download in PR 209 was required; the contract branch alone downloaded the wrong architecture and failed with `exec format error`.

The integrated Magnolia commit has not been pushed as a release or used to move the pin.

## 4. Required integration before release

`exp/dreaming-pass` has advanced since the authentication candidate was verified:

```text
fc18124f feat(memory): expose the judge entry point and identity
d390463f fix(agents): deny service files through every file tool
069dd67b fix(memory): fail dreaming on malformed judge responses
45cc4adb fix(memory): reject a judge response that carries no valid verdict
```

The remaining release gates are joint decisions rather than more implementation:

1. Review the integrated commit and the SC-25 Dockerfile fix.
2. Complete the outstanding Collegium boundary reviews, including PRs #207–#210 and #218.
3. Decide the tag, pin move, and participant-notice activation jointly.

## 5. Limits of the existing evidence

- Contract tests use real Magnolia modules and real servers, but stand-ins remain for several lifecycle paths. They do not replace a live interactive client turn or long-lived real-service reuse.
- The earlier second-run SC-25 timeout did not recur, so it is not fully explained. A green retry is not proof that the timeout is impossible.
- Budget supervision is tested synthetically. No real paid-provider stream has verified live budget stopping or reconciliation.
- No provider-side hard spending limit has been established by this authentication work.
- Some earlier evidence lived in temporary worktrees that have since been removed. Final approval should use a fresh integrated run rather than rely on those paths alone.
- The current pin, `553a04e`, does not contain the newer judge, permission, or authentication changes.

## 6. Suggested final verification commands

Use an integrated authentication worktree and the exact resulting SHA:

```bash
COLLEGIUM_MAGNOLIA_DIR=/tmp/opencode/magnolia-tool-service-auth \
COLLEGIUM_MAGNOLIA_REF=<integrated-auth-sha> \
scripts/contract.sh
```

Run the full offline release gate in the Collegium worktree, then run:

```bash
scripts/lab.sh run SC-25 --commit <integrated-auth-sha>
```

Do not activate the participant notice before the pin and final guardrails are approved.

## 7. Release decision still needed

Joint review must decide:

- Whether the integrated commit is the release candidate.
- Whether all boundary reviews are complete.
- Which commit or tag becomes the new Collegium pin.
- When the participant notice may be activated.

This packet changes no runtime configuration and activates nothing.
