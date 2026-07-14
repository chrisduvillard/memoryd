# One-Command Hermes Installation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development and superpowers:test-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Release memoryd v0.3.1 with a safe, interactive `memoryd install --hermes` workflow for the pinned Hermes Agent 0.16.0 Linux runtime.

**Architecture:** Keep generic `memoryd install` unchanged and add a Linux-only orchestration module that composes existing database, migration, daemon, systemd, backup, and plugin primitives. Package the existing Hermes contract/lifecycle validation logic so the guided installer can run it through the interpreter behind the installed `hermes` command before target mutation. Activation is transactional: snapshot the provider and gateway state, activate and verify, then restore prior state on every exception or interruption.

**Tech Stack:** Python 3.11+, pytest, standard-library subprocess/getpass/urllib/pathlib, PostgreSQL 16 + pgvector, systemd user services, pipx, Hermes Agent 0.16.0.

## Global Constraints

- Public guided command is exactly `memoryd install --hermes`; unknown install arguments exit 2.
- Existing cross-platform `memoryd install` behavior remains unchanged.
- Guided mode supports Linux with a working systemd user manager only.
- Hermes Agent must be package version `0.16.0`, source tag `v2026.6.5`, resolved commit `3c231eb3979ab9c57d5cd6d02f1d577a3b718b43`; drift is a non-mutating hard stop.
- Secrets are never accepted as command-line arguments or printed. Missing OpenRouter and Voyage keys use hidden interactive input and both are required.
- Guided configuration is `MEMORYD_LLM=openrouter`, `MEMORYD_EMBED=voyage`, and `http://127.0.0.1:7437`.
- The selected Hermes profile and memoryd configuration are owner-only. Invalid profiles, unsafe permissions, path ambiguity, or unknown nonempty memory homes are refused before target mutation.
- Hermes mode does not register Claude Code hooks.
- Activation failure or interruption restores the previous Hermes provider and gateway state and preserves every memoryd database, spool, archive, configuration, backup, and log artifact.
- Windows state and data are untouched. `graphify-out/` remains untracked and preserved.
- Production code follows strict RED-GREEN-REFACTOR TDD with focused tests and pristine output.

---

### Task 1: Package Hermes discovery and compatibility validation

**Files:**
- Create: `memoryd/hermes_compat.py`
- Create or move implementation into: `memoryd/hermes_validation/`
- Modify: `scripts/check_hermes_contract.py`
- Modify: `scripts/validate_installed_hermes.py`
- Modify: `pyproject.toml`
- Test: `tests/test_hermes_compat.py`
- Test: existing contract and installed-runtime validator suites

**Interfaces:**
- Produce immutable constants `PINNED_HERMES_VERSION`, `PINNED_HERMES_TAG`, and `PINNED_HERMES_COMMIT`.
- Produce frozen `HermesTarget(root: Path, home: Path, executable: Path, python: Path)`.
- Produce `resolve_hermes_home(environ: Mapping[str, str] | None = None) -> tuple[Path, Path]` where the tuple is root and authoritative profile home.
- Produce `resolve_hermes_target(environ: Mapping[str, str] | None = None) -> HermesTarget`.
- Produce `validate_hermes_compatibility(target: HermesTarget, plugin_source: Path) -> None` that runs the packaged contract and isolated lifecycle checks with `target.python`.

- [ ] Write focused tests for explicit profile paths, absent/default active profile, valid named active profile, invalid/multiline/traversal profile names, missing named profile, relative paths, symlinks/path ambiguity, non-0700 profile permissions, missing Hermes command, malformed/non-executable shebang, package-version mismatch, child-validator failure, and success.
- [ ] Run the focused tests and record expected failures caused by missing interfaces.
- [ ] Move/refactor the current validator logic into importable wheel-packaged modules while keeping both `scripts/*.py` entry points as thin compatible wrappers.
- [ ] Ship the pinned `MemoryProvider` source with the package and ensure the validators receive the wheel-bundled plugin path, never an arbitrary checkout copy.
- [ ] Resolve the interpreter from the real `hermes` executable safely on Linux, query `importlib.metadata.version('hermes-agent')` in that interpreter, and reject anything except `0.16.0` with the exact pinned remediation command.
- [ ] Run contract, validator, wheel-content, and focused tests until green; then run the full suite once.
- [ ] Commit with a focused compatibility/preflight message and write TDD evidence to the task report.

### Task 2: Add secure guided configuration and safe core installation

**Files:**
- Create: `memoryd/hermes_install.py`
- Modify: `memoryd/cli.py`
- Test: `tests/test_hermes_install.py`
- Test: existing install and backup unit suites

**Interfaces:**
- Produce `HermesInstallError(RuntimeError)` with already-redacted user-facing messages.
- Produce frozen `ProviderCredentials(openrouter_key: str, voyage_key: str)`.
- Produce `collect_provider_credentials(config_path: Path) -> ProviderCredentials` using environment, recognized owner-only config, then `getpass.getpass` for missing values.
- Produce `validate_provider_credentials(credentials: ProviderCredentials) -> None` using a minimal OpenRouter completion and Voyage embedding through existing clients; wrap all failures without response bodies or secrets.
- Produce `classify_memory_home(home: Path) -> Literal['fresh', 'managed']`, refusing every other existing/nonempty shape.
- Produce `install_hermes_core(target: HermesTarget, credentials: ProviderCredentials) -> None` that composes the existing installer without Claude hooks and verifies one initial backup.

- [ ] Write tests for TTY/Linux/systemd prerequisites, operator confirmation, environment/config/prompt credential precedence, missing/empty credentials, no secret echo, provider success/failure redaction, fresh/recognized/unknown homes, owner-only config, Hermes-mode install ordering, skipped Claude hooks, localhost settings, idempotent rerun, systemd backup failure, snapshot verification failure, and success.
- [ ] Run tests and record expected RED failures.
- [ ] Add a target-mutation boundary: platform, TTY, Hermes compatibility, profile/home safety, operator confirmation, credential collection, and live provider probes all finish before database/profile/config mutations.
- [ ] Reuse `ensure_container`, migrations, `write_config`, plugin copying, systemd installation, daemon startup, and status primitives. Pass the authoritative profile explicitly instead of relying on ambient profile re-resolution.
- [ ] Persist the two keys plus `MEMORYD_LLM=openrouter` and `MEMORYD_EMBED=voyage` only through the existing atomic owner-only configuration writer.
- [ ] Start and wait for the installed backup service, identify its newly-created snapshot, verify it, and require the daemon to return healthy before activation.
- [ ] Run focused and existing install/backup tests to green, then the full suite once.
- [ ] Commit and record TDD evidence.

### Task 3: Implement transactional Hermes activation and rollback

**Files:**
- Modify: `memoryd/hermes_install.py`
- Test: `tests/test_hermes_activation.py`
- Test: Hermes spool/lifecycle suites

**Interfaces:**
- Produce frozen `HermesRuntimeState(provider: str | None, gateway_running: bool)`.
- Produce `capture_runtime_state(target: HermesTarget) -> HermesRuntimeState`.
- Produce `activate_and_verify(target: HermesTarget) -> None`.
- Produce `guided_hermes_install() -> int`, returning 0 only when installation, backup, activation, all health checks, and gateway restoration succeed.

- [ ] Write tests for provider-state capture, gateway inactive/active detection, stop verification, activation, exact four-command verification order, queue/dead-letter/durability failure propagation, successful gateway restart, failure at each step, rollback to a previous external provider, rollback to built-in-only, rollback restart failure, and `KeyboardInterrupt`/termination cleanup.
- [ ] Run tests and record expected RED failures.
- [ ] Capture the provider before changing it and inspect the gateway with the installed Hermes interpreter. Stop only an active gateway and verify it stopped.
- [ ] Activate with `hermes config set memory.provider memoryd`, verify the authoritative profile configuration, then run `hermes memory status`, `hermes memoryd config`, the in-process memoryd status equivalent, and `hermes memoryd status`.
- [ ] Treat any nonzero exit, wrong URL, dead letter, durability fault, or non-draining queue as failure.
- [ ] Use `try/finally` transaction semantics so exceptions and interruptions restore the prior provider (or `hermes memory off`) and the original gateway state. Preserve memoryd artifacts and return nonzero with evidence if rollback itself is incomplete.
- [ ] On success, print the selected profile, daemon URL, backup snapshot, healthy checks, restored gateway state, and existing canary instructions without printing secrets.
- [ ] Run focused activation, spool, lifecycle, and full tests to green.
- [ ] Commit and record TDD evidence.

### Task 4: Wire the CLI, documentation, release metadata, and CI gates

**Files:**
- Modify: `memoryd/cli.py`
- Modify: `memoryd/__init__.py`
- Modify: `README.md`
- Modify: `docs/HERMES_INSTALL_PROMPT.md`
- Modify: `docs/PRODUCTION_ROLLOUT.md`
- Modify: release/CI assertion tests as needed
- Test: `tests/test_cli.py` or the existing CLI unit file
- Test: release/runbook/CI matrix suites

**Interfaces:**
- `memoryd install` remains the existing generic installer.
- `memoryd install --hermes` calls `guided_hermes_install()`.
- `memoryd install <anything-else>` prints `usage: memoryd install [--hermes]` to stderr and exits 2.
- Package version becomes `0.3.1`.

- [ ] Write CLI tests first for no arguments, exact `--hermes`, unknown flags, extra positional arguments, exit-code propagation, and no secret arguments.
- [ ] Run them and record RED failures.
- [ ] Add minimal exact argument dispatch without replacing the repository's established hand-written CLI style.
- [ ] Put the immutable-tag two-command quickstart prominently in the README and explain prerequisites, separate-terminal/TUI shutdown, secure prompts, profile selection, reruns, rollback, backups, and canary limits.
- [ ] Simplify the Hermes prompt to direct the operator to exit Hermes and run the guided command in a normal terminal; it must forbid Hermes from activating or restarting its own provider.
- [ ] Retain the detailed runbook as the audit/troubleshooting source and update version/tag references to v0.3.1 without weakening the 14-day/200-turn promotion gates.
- [ ] Update release assertions and wheel tests to prove validators, pinned contract, migrations, and plugin ship in Python 3.11 and 3.13 wheels.
- [ ] Run focused docs/release/CLI tests, build and inspect the wheel, install it in clean Python 3.11 and 3.13 environments where available, and run the full suite.
- [ ] Commit and record TDD evidence.

## Whole-Branch Verification and Release

- [ ] Generate one merge-base review package and obtain an independent whole-branch spec/security/code-quality review; fix and re-review every Critical or Important finding.
- [ ] Run compilation, full pytest, all standalone regression scripts, wheel installation/migration checks, and real pinned Hermes compatibility/lifecycle validation.
- [ ] Run a disposable Linux Docker/systemd happy path and forced activation-failure rollback when a Linux host is available; do not simulate success on Windows.
- [ ] Push `codex/hermes-simple-install`, open a PR, wait for and repair all GitHub checks, independently review the final PR diff, merge, and verify post-merge main CI.
- [ ] Tag the verified merge commit `v0.3.1`, publish the GitHub release, and verify installation from the immutable tag. Do not claim canary promotion before the existing 14-day/200-turn gate passes.
