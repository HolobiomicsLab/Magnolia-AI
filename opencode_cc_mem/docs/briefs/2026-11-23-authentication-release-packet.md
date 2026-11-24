# Tool-service authentication release packet

Date: 23 November 2026
Status: review evidence only — **no tag, pin move, deployment, or notice activation**

## 1. Exact references

| Item | Reference |
| --- | --- |
| Current Collegium pin | `553a04e8ac0aa49fd1f61d05118a1e469b8eb6fb` |
| Authentication candidate | `7f0211c46ca9ab43d5f25cbbf572521fe2e23d59` |
| Current `exp/dreaming-pass` head | `fc18124f8cd2b4dc217bb53271d1cfe1eecad273` |
| Contract-test branch | `7be6063032637e9ff77e81e87190e6c9ad851188` |

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

## 3. Required integration before release

`exp/dreaming-pass` has advanced since the authentication candidate was verified:

```text
fc18124f feat(memory): expose the judge entry point and identity
d390463f fix(agents): deny service files through every file tool
069dd67b fix(memory): fail dreaming on malformed judge responses
45cc4adb fix(memory): reject a judge response that carries no valid verdict
```

Before tagging or moving the pin:

1. Merge or rebase the authentication candidate onto `fc18124f`, preserving the exact authentication behavior.
2. Run the real-Magnolia contract suite against the resulting commit.
3. Run the full offline release gate against that same commit.
4. Re-run live x86_64 SC-25 on that exact commit and require 40 of 40 checks.
5. Complete the outstanding Collegium boundary reviews, including PRs #207–#210 and #218.
6. Only then decide the tag, pin move, and participant-notice activation jointly.

## 4. Limits of the existing evidence

- Contract tests use real Magnolia modules and real servers, but stand-ins remain for several lifecycle paths. They do not replace a live interactive client turn or long-lived real-service reuse.
- The earlier second-run SC-25 timeout did not recur, so it is not fully explained. A green retry is not proof that the timeout is impossible.
- Budget supervision is tested synthetically. No real paid-provider stream has verified live budget stopping or reconciliation.
- No provider-side hard spending limit has been established by this authentication work.
- Some earlier evidence lived in temporary worktrees that have since been removed. Final approval should use a fresh integrated run rather than rely on those paths alone.
- The current pin, `553a04e`, does not contain the newer judge, permission, or authentication changes.

## 5. Suggested final verification commands

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

## 6. Release decision still needed

Joint review must decide:

- Whether the integrated commit is the release candidate.
- Whether all boundary reviews are complete.
- Which commit or tag becomes the new Collegium pin.
- When the participant notice may be activated.

This packet changes no runtime configuration and activates nothing.
