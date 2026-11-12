# Dependency advisory audit

`.github/workflows/dependency-audit.yml` fails a run when the dependency set the
bot container installs carries a critical or high advisory.

There is no lock file here to scan. `pyproject.toml` declares ranges
(`python-telegram-bot>=22.7,<23`), `core-lock.json` pins the Core wheel's
SHA-256 and not its dependency graph, and `deploy/Dockerfile` lets pip resolve
at image build time. The object worth auditing is therefore not a lock but the
pinned list today's resolution installs, which is what the bot process loads.

## What it audits

The job obtains the same two wheels the release flow uses — the pinned Core
wheel through the lock, the Telegram wheel from `python -m build` — and installs
them through `scripts/install_verified.py`, the installer CI and the image use.
It then runs `pip check`, freezes the environment, and drops the two `wuwaterm`
distributions from the freeze. Those are local wheels rather than PyPI
releases, and `pip freeze` writes them as `<name> @ file://...`, which would
also put a local path into the uploaded report. What remains is the runtime
set; at the time of writing it is eight pinned packages.

A green audit over a short or empty list is the failure this gate exists to
prevent, so the input is checked before it is trusted: fewer than four pins, no
`python-telegram-bot`, or a `wuwaterm` line in the set fails the run.

`pip-audit`, pinned to `2.10.1`, reads the frozen pins with `--no-deps` from its
own environment, so its own dependencies never join the set being audited.

## What fails it

- A critical or high advisory. The fix is to raise the floor in
  `pyproject.toml`, or add a constraint for a transitive dependency, and record
  why the advisory does not apply. Editing the gate to go green is not one.
- A severity that cannot be read. `pip-audit` names advisories but not their
  severity, so the gate reads GitHub's label from the OSV record
  (`database_specific.severity`), asking for a `GHSA-` identifier first and
  collapsing records that share one identifier. An advisory nobody can classify
  is treated as blocking. The lookup runs only for advisories `pip-audit`
  reported, so a clean run makes no request.
- A report that cannot be trusted: missing, empty, truncated or malformed JSON,
  a dependency `pip-audit` could not collect, or a `pip-audit` exit status other
  than `0` or `1` — including `1` with no advisory in the report. This ends at
  exit `2`, because a green result over a set nobody read is not a result.

Medium and low advisories are printed with their advisory IDs and fix versions
and do not fail the run.

## When it runs

Every push to `main`; a pull request that changes one of the resolver or gate
inputs (`pyproject.toml`, `core-lock.json`, `scripts/install_verified.py`,
`scripts/verify_wheels.py`, `scripts/fetch_core.py`, `scripts/core_lock.py`,
`scripts/check_dependency_advisories.py`, `tests/test_dependency_advisories.py`
or the workflow itself); the weekly schedule, Tuesdays 03:41 UTC; and manual
dispatch. Advisories are published against versions that never change, so a
week without a commit can still turn the run red.

The job is deliberately not part of `ci.yml`, and it must not be a required
status check. `pull_request.paths` is a workflow-level filter: a pull request
that touches no dependency file never starts this workflow, so requiring the
check would leave that pull request waiting for a status that will never
arrive. Hanging the job off `ci.yml` instead would skip the package build and
the test suite on every non-dependency pull request, rebuild the package once a
week for the schedule, and gate the release flow — which reuses `ci.yml` — on
OSV availability.

## What it does not cover

- The test and build toolchain (`pytest`, `ruff`, `build`); it is not installed
  into the runtime set.
- OS packages in the base image and anything `deploy/Dockerfile` adds outside
  pip.
- Pinned GitHub Actions versions.
- Build-time dependencies: the build backend resolves its own requirements
  while the wheel is built.
- Advisories published after a run; the next weekly run is what picks them up.

## Running it locally

From a clean checkout, these mirror the workflow's steps. The build tool gets
its own venv so a managed system Python is enough; the venv paths and `status`
stand in for the runner's `$RUNNER_TEMP` and `$PIP_AUDIT_STATUS`.

```bash
set -euo pipefail

python scripts/fetch_core.py --lock core-lock.json --output-dir artifacts

python -m venv .audit-build
.audit-build/bin/python -m pip install --disable-pip-version-check build
.audit-build/bin/python -m build

python -m venv .audit-runtime
core=(artifacts/*.whl); telegram=(dist/*.whl)
sha=$(sha256sum "${telegram[0]}" | cut -d' ' -f1)
.audit-runtime/bin/python scripts/install_verified.py \
  --lock core-lock.json --core "${core[0]}" \
  --telegram "${telegram[0]}" --telegram-sha256 "$sha"
.audit-runtime/bin/python -m pip check
.audit-runtime/bin/python -m pip freeze > installed.txt
grep -v '^wuwaterm' installed.txt > runtime-set.txt
count=$(grep -c '==' runtime-set.txt || true)
echo "resolved ${count} pinned packages"
test "$count" -ge 4
grep -q '^python-telegram-bot==' runtime-set.txt
! grep -q '^wuwaterm' runtime-set.txt

python -m venv .audit-tool
.audit-tool/bin/python -m pip install --disable-pip-version-check 'pip-audit==2.10.1'
status=0
.audit-tool/bin/python -m pip_audit -r runtime-set.txt \
  --no-deps --strict --progress-spinner off --format json -o pip-audit.json || status=$?
python scripts/check_dependency_advisories.py pip-audit.json --audit-exit "$status"
```

A leftover `build/` directory from an earlier local build shadows the module,
so start from a clean checkout or remove it before the build step.
`installed.txt`, `runtime-set.txt`, `pip-audit.json` and the three venvs are
not ignored by git; remove them when done.

## Reading a failure

The job is `resolved runtime dependencies (critical/high)`. Each blocking
advisory gets an annotation naming the package, the advisory ID and the fix
version. The run keeps `dependency-audit-report-<run_attempt>` — `pip-audit.json`,
`runtime-set.txt` and `installed.txt` — on success and on failure, so the report
is the evidence for both outcomes. The attempt number is in the name because an
artifact name must be unique within a run: without it, re-running a failed job
after the advisory was fixed would fail on the upload instead of reporting the
new audit.
