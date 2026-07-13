# Task 2B report: safe Hermes core install and verified initial backup

## Scope delivered

- Added `install_hermes_core(target, credentials) -> Path` without adding public
  `--hermes` dispatch or provider activation.
- Added a private `cli._InstallOptions` path while preserving the zero-argument
  `cli.install()` sequence and output.
- Hermes mode revalidates the selected Hermes root/profile and the memoryd home
  immediately before installation, targets the explicit profile, skips Claude
  hooks, scopes/restores provider environment, and requires healthy install
  status.
- The initial backup snapshots the existing generated listing, starts and waits
  for `memoryd-backup.service`, requires exactly one new snapshot, verifies it,
  then requires daemon health after the unit's restart.
- Failures are stage-sanitized and do not clean up database, home, plugin, spool,
  archive, log, or backup evidence.

## TDD evidence

All commands ran in Ubuntu Linux through WSL with uv-managed CPython 3.11.

### RED

Command:

```text
uv run --python 3.11 --no-project --with pytest --with 'psycopg[binary]>=3.2' --with 'psycopg_pool>=3.2' python -m pytest -q tests/test_hermes_install.py -k core_install
```

Authoritative result before production edits:

```text
FFFFFFFFFFF                                                              [100%]
11 failed, 48 deselected in 1.03s
```

Every selected failure was the intended missing behavior:
`AttributeError: module 'memoryd.hermes_install' has no attribute
'install_hermes_core'`. A preliminary run exposed a test-only missing `sys`
import; it was corrected before the authoritative RED run above.

### GREEN

The same focused command after implementation:

```text
...........                                                              [100%]
11 passed, 48 deselected in 0.38s
```

The existing generic install test then caught a backward-compatibility defect
in the first refactor: `install_hermes_plugin(None)` broke a zero-argument test
double.

```text
python -m pytest -q tests/test_install_backup_units.py::test_install_reuses_working_config_dsn_without_docker
F                                                                        [100%]
1 failed in 0.20s
```

After restoring the exact zero-argument helper call on generic installs:

```text
.                                                                        [100%]
1 passed in 0.20s
```

## Required verification

### Focused Linux Python 3.11 install/backup suite

Command:

```text
uv run --python 3.11 --no-project --with pytest --with 'psycopg[binary]>=3.2' --with 'psycopg_pool>=3.2' python -m pytest -q tests/test_hermes_install.py tests/test_install_backup_units.py tests/test_backup.py
```

Result:

```text
175 passed in 2.59s
```

### Full Linux Python 3.11 suite

The first isolated-runner attempt stopped during collection because PyYAML was
not installed (`ModuleNotFoundError: No module named 'yaml'` in
`tests/test_ci_matrix.py`); no tests executed. The runner dependency was added
without changing the repository, then the full suite was rerun with:

```text
uv run --python 3.11 --no-project --with pytest --with pyyaml --with 'psycopg[binary]>=3.2' --with 'psycopg_pool>=3.2' python -m pytest -q
```

Result:

```text
386 passed, 1 skipped, 22 subtests passed in 41.50s
```

`git diff --check` also completed with exit code 0 and no output.

## Self-review

- Ordering: compatibility/provider/operator preflight remains outside this
  mutation function; the function re-resolves the authoritative profile and
  reclassifies the memoryd home before the first install mutation.
- Generic compatibility: `install()` without options still registers Claude
  hooks, calls `install_hermes_plugin()` with no argument, installs autostart,
  starts/waits for the daemon, prints the same lines, and returns `status()`.
- Hermes isolation: the private options object carries the explicit
  `target.home`; hooks are skipped only on this path; plugin source remains the
  existing wheel/source `_resource_dir("hermes_plugin")`; private config remains
  the existing atomic exact `http://127.0.0.1:7437` writer.
- Provider persistence: only the specified installation values are overlaid;
  existing atomic `write_config` persists the OpenRouter/Voyage keys and
  selectors; all five scoped variables are removed/restored in `finally`.
- Failure safety: underlying exception/service/verification details are never
  included in operator errors; no failure branch deletes or rolls back evidence.
- Backup proof: both listings use the existing generated-snapshot primitive,
  set difference must contain exactly one path, explicit `verify_snapshot` must
  return `ok`, and daemon health is checked after the backup service finishes.
- Scope: no activation, public CLI dispatch, documentation, version, uninstall,
  migration, or backup implementation changes were made.

No unresolved correctness concern was found in the scoped diff.

## Independent review

A read-only reviewer checked the uncommitted diff against the Task 2B brief and
reported **Ready: Yes**, with no Critical, Important, or Minor issues.
