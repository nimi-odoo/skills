---
name: reprodoo
description: Reproduce an Odoo bug locally against exact branch code (Python ORM/RPC or JS/UI) for a fast debug loop. Use proactively whenever debugging Odoo behavior — staging errors, local failures, or verifying a fix.
---

# Reproduce staging-branch Odoo bugs locally

Workflow + tooling for reproducing Odoo runtime bugs on this machine: the branch's code in a worktree of the repro's own, run against a local database, never the staging server itself. Built around `repro.py`, one of the scripts shared by this repo's skills. They live at:

```bash
$(realpath "${CLAUDE_SKILL_DIR}/../../scripts")
```

Below, `$SCRIPTS` stands for that path. Write it out in each command, since shell variables don't carry over between commands.

## Setup check

Paths to Odoo, Enterprise, the venv and the project repos come from the user's workspace conf, one section per Odoo version. `/setup-nimi-skills` writes it and records its path in a `## nimi-skills` section of the user's CLAUDE.md. Pass that path as `--conf=<path>` on every script call, unless `$ODOO_WORKSPACE_CONF` is set.

If there's no such section and no `$ODOO_WORKSPACE_CONF`, stop and ask the user to run `/setup-nimi-skills`. Don't write a conf yourself.

Before the first run in a session:

```bash
python3 $SCRIPTS/odoo_workspace.py show --conf=<path> [--target=<worktree-path>]
```

It prints the resolved paths and the version it picked, with the reason.

### Odoo version

Both scripts pick the conf section themselves and print `Odoo <version> (<reason>)` at the start of each run. Leave `--odoo-version` off by default. Pass it only when:
- the user names the version, or
- a run stops because the version is unclear (no version in the manifests or branch name, modules declaring several versions, or manifests and branch name disagreeing). **Ask the user which version to use**, quoting what the error found. Don't pick one yourself.

If the printed version looks wrong for what the user described (e.g. `only version in the conf` for a branch they called an 18.0 fix), stop and ask before going further.

## When to use

User reports an error on a staging/dev branch ("<staging-branch> broken for cashiers", "got a 500 on <staging-branch>", "POS shows the wrong button"…). The bug is reachable by running the same code path locally — Python (any RPC, ORM, compute, constraint) or JS (any browser-driven flow).

## Inputs

Extract from the user's free-text invocation:
- **Branch name** (e.g. `<staging-branch-experiencing-issue>`, `19.0-fix-foo-bar`), or a commit or worktree path to start from. Required.
- **Actor / role** ("cashier", "portal user", "manager", or specific group XML IDs). Required. Map a role to groups from the module's `res.groups` records (`security/*.xml`), or the app's own groups when the module defines none (e.g. `stock.group_stock_manager` for an inventory manager), and tell the user which groups you picked. With `pos_hr`, a POS cashier is usually an employee with a PIN rather than a user, so create the employee and add it to the POS config's employee lists.
- **Action** ("opens POS", "clicks checkout", "calls `pos.session.load_data`", "submits the contact form"). Required.
- **Error text or symptom** (alert string, console error, traceback frame). Helpful — the repro produces the canonical version.
- **Project repo**, only when it isn't one of the conf's `projects` → `--repo=<path>`.

From these, pick a **name** for the repro: a short slug for what's being reproduced, like `cashier-refund-crash` or `portal-invoice-403`. Pass it as `--name=<slug>` on every run of this investigation, and tell the user which name you picked. It names the database, so it should say what the bug is, not which branch it's on.

If any required field is missing, ask once with all gaps in a single question.

## The repro's worktree

Every repro runs in a worktree of its own, `<version>-repro-<name>` (e.g. `19.0-repro-cashier-refund-crash`), created by `repro.py` in the job's temp dir (`$CLAUDE_JOB_DIR/tmp`, or the system temp dir outside a job). The code it starts from is never edited: not the user's worktree for that branch, and not their main checkout. The first argument (`<target>`) only says where to start:

1. **A branch name** (the usual case, e.g. `<staging-branch-experiencing-issue>`) → starts from `origin/<branch>`, fetched first: the code that's deployed. If the user's local branch has commits origin doesn't, the script prints a note; mention it, since those commits aren't in the repro.
2. **A path to an existing worktree** (e.g. one a PR review left behind) → starts from that worktree's current commit. Its uncommitted changes aren't carried over.
3. **`--ref=<ref or commit>`** → starts from anything else, such as a commit a user says introduced the bug.

The run prints `Worktree:` (the repro's own) and, when there is one, `Source worktree:`: the user's worktree the repro started from, either the path given or the worktree that has the branch checked out. The source is only ever changed in the hand-over step below, and only if the user says so.

Don't create worktrees by hand, and never add one to the user's own worktree folder. The repro's worktree also gets its git submodules filled, as Odoo.sh does.

Each repro gets its own database, `odoo-repro-<name>`, paired with that worktree, both reused on every run with the same `--name`, so a fix applied in the worktree survives the next run. The database is tagged with the repo and branch it was built from. If the name is already taken by a database built from other code, the script moves on to `<name>-2` (then `-3`, …) for both and prints the name it used, so two bugs that got the same slug never share setup or code. Without `--name` they're named after the repo and branch. Names are cut to Postgres' 63-byte limit, ending in a short hash when cut.

To start again from clean code, `git -C <project-repo> worktree remove --force <worktree>` and re-run.

## Principle

Don't diagnose from grep'd code or alert text alone. Build a small, fast, deterministic local reproduction; let it raise the actual exception (or show the actual wrong UI state); diagnose from that signal.

Reading code is how you design the repro, not how you conclude. The recent diff, the module's tests and the views show what data, settings and steps reach the failing path; the repro is what proves it.

Reproduce the whole flow the user describes, not just the screen they name. An error "on a menu" may only come from opening a record in it, pressing a button, or saving, so walk the path the user took.

The cause can be configuration as much as code: a setting, a user's groups, a record set up a certain way. Settings are part of the setup, so mirror them in the setup script like any other data.

If the first repro doesn't trigger the bug, the issue is missing setup — wrong modules, wrong user groups, wrong data, or a step of the flow skipped — not your hypothesis. Tighten the setup before changing direction.

## Choosing the path

| Bug shape | Path |
|---|---|
| Python exception named in the report (AccessError, IndexError, KeyError, traceback frames, RPC error data) | **shell mode** — see below |
| JS error in console, UI doesn't render, click does nothing, OWL crash, "Cannot read property of undefined" | **serve mode** + Chrome — see below |
| Ambiguous | start with shell mode; if the equivalent server-side call succeeds, escalate to serve mode |

Often a bug surfaces as a JS alert but is actually a Python exception serialised through an RPC error. If the user has a way to view the failing request's response payload (DevTools Network → response → `error.data.debug`), use that to disambiguate. Otherwise default to shell mode first — it's faster.

## Path A: shell mode (Python-reachable)

`repro.py` runs the bug script inside `odoo-bin shell --no-http`. The script gets `env` as a global.

```bash
# from a branch (origin/<branch>) or an existing worktree's commit
python3 $SCRIPTS/repro.py <target> $CLAUDE_JOB_DIR/tmp/repro_<name>.py --conf=<path> --name=<slug> \
    [--repo=PATH] [--modules=A,B,C] [--fresh] [--odoo-version=V]

# from any other ref or commit
python3 $SCRIPTS/repro.py --ref=<ref> \
    $CLAUDE_JOB_DIR/tmp/repro_<name>.py --conf=<path> --name=<slug> [--modules=A,B,C] [--fresh]
```

Bug script template (`$CLAUDE_JOB_DIR/tmp/repro_<name>.py`; `mkdir -p $CLAUDE_JOB_DIR/tmp` first if it doesn't exist):
```python
from repro import as_user, isolate_field_read

user = as_user(env, 'repro_user', ['base.group_user', 'point_of_sale.group_pos_user'])  # password: repro_user
config = env['pos.config'].search([], limit=1) or env['pos.config'].create({...})

try:
    result = config.with_user(user).some_method(args)
    print(f"OK: {result}")
except Exception as e:
    print(f"RAISED: {type(e).__name__}: {e}")
    # diagnostics — see helpers below
```

Read records the way the failing screen does. A form reads one record, so use `browse(id)`; looping over a whole `search()` computes fields for the batch at once, so one bad record makes every record in it raise, which the browser won't show.

### Diagnostics for shell mode

When the script raises, reach for the helper matching the error class:

- `AccessError` → `isolate_field_read(record, user)` to find which field's read raises. Returns `[(fname, exc_or_None), ...]`.
- `IndexError` on `data['model'][0]` inside a `load_data` chain → an earlier model's load swallowed an `AccessError`. Repro that model directly: `env[model].with_user(user)._load_pos_data_search_read({}, config)`.
- `spy_method(env, model, method_name, stack_depth=5)` → monkey-patch to print args + partial stack each call. Returns a restore fn.
- Anything else → `traceback.print_exc()`.

## Path B: serve mode (JS / UI-reachable)

`repro.py --serve` brings the HTTP server up on **port 8269** (8069 = dev, 8169 = `run_tests.py`, 8269 = reprodoo, so all three can run at once). `--port` is the lowest port to try: when it's taken the script climbs to the next free one and prints where it landed, so parallel serves each get their own.

Start it with `run_in_background`, sending its output to a log, and keep it alive while you drive the browser:

```bash
python3 $SCRIPTS/repro.py <target> --serve --conf=<path> --name=<slug> \
    [--repo=PATH] [--fresh] [--port=8269] [--odoo-version=V] > $CLAUDE_JOB_DIR/tmp/serve_<slug>.log 2>&1

# or, from any other ref or commit
python3 $SCRIPTS/repro.py --ref=<ref> --serve --conf=<path> --name=<slug> > $CLAUDE_JOB_DIR/tmp/serve_<slug>.log 2>&1
```

Then wait for it to end on `>>> READY` or `>>> FAILED`:

```bash
until grep -qE ">>> (READY|FAILED)" $CLAUDE_JOB_DIR/tmp/serve_<slug>.log; do sleep 1; done; grep ">>> " $CLAUDE_JOB_DIR/tmp/serve_<slug>.log
```

The line reads `>>> READY: http://odoo-repro-<name>.localhost:<port>/web (db=...)`. Take the URL from it rather than assuming a port.

To stop the server: `pkill -f "odoo-bin.*--http-port=<port>"`, with the port from the READY line. Odoo prints cron-thread tracebacks while shutting down; they're harmless.

### Driving Chrome

Use the `mcp__claude-in-chrome__*` tools. First-time pattern:
1. `tabs_context_mcp` with `createIfEmpty: true` to get the MCP tab group, then `tabs_create_mcp` for a tab of your own. Work only in that tab, even if the group already has others: another agent may be using them.
2. `navigate` to the URL from the READY line, e.g. `http://odoo-repro-<name>.localhost:<port>/web/login`. Always use that hostname, never plain `localhost` or `127.0.0.1`: browsers share cookies across every port of a host, so two repros on the same host log each other out. Each repro's server also only accepts its own database. Ignore tabs left from earlier runs, even ones on your port.
3. Log in as the user your setup script created. `as_user()` gives it a password equal to its login, so `repro_user` / `repro_user`. After `navigate`, take a screenshot and wait until the login form is visible; typing into a page that's still loading is silently lost. Then run `find` for the fields and button (references from an earlier `find` go stale), type both, and click **Log in**; pressing Return doesn't always submit. Confirm the login worked from the URL or a screenshot before going on.
4. `navigate` to the bug's URL (e.g. `/pos/ui?config_id=N`). The first load after the server starts or assets change can show an empty page for up to ~15s while bundles build. Wait until the page shows a form, list, POS screen or error dialog before judging the result or calling `find`.
5. `read_console_messages` with `onlyErrors: true` (and a `pattern` to narrow noise) to capture the actual JS error. Pass `clear: true` before reloading to check a fix, or the old errors come back and look like the fix failed. The clearing call itself returns the messages it clears, so read the console again after the reload.
6. `read_page` (or `read_network_requests`) for additional state

Batch sequences via `browser_batch` — significantly faster than individual calls. When done, close only the tab you created.

### Mixing Path A and Path B

Often you need server-side setup (create a user, create a config, commit) *and* a browser flow. Do them in two steps:

1. Run a small shell-mode script to set up users/data and `env.cr.commit()`. Write it to be safe to re-run (search for a record before creating it), since you'll run it again after changes.
2. Start `--serve` with the same `--name` (DB is reused, setup is now visible to HTTP requests).
3. Drive Chrome.
4. To inspect server state mid-test, run another shell-mode script — same DB.
5. After changing data or settings from a shell-mode script, restart `--serve`. The running server caches settings (system parameters in particular) and doesn't see a shell script's commit.

## Workflow

1. **Gather inputs** (above). Ask once for gaps. Pick the repro's name.
2. **Choose path** from the table, and the **starting point** from the three cases above.
3. **Write `$CLAUDE_JOB_DIR/tmp/repro_<name>.py`** (Path A) or open a Chrome tab to the server (Path B).
4. **Run** — first invocation takes ~40s (DB create + module install); subsequent runs ~10s. Use `--fresh` only when DB drift is plausible.
5. **Diagnose** with the helpers / Chrome MCP tools above.
6. **Report the cause.** For code: the file and line, and why it fails. For configuration: the setting and value that trigger it, and whether the code should cope with that setting anyway (a crash on a valid setting is usually a code bug too).
7. **Apply the fix.** A code fix goes in the repro's worktree (the `Worktree:` line), never in the user's. A configuration fix is applied to the repro database, in a small shell-mode script, then `--serve` is restarted; the code stays as it is.
8. **Re-run the same repro** (same `--name`, no `--fresh`) to verify it's now green. In serve mode, any code fix (Python or JS) needs the server restarted: stop it, start `--serve` again, and reload the page at the new READY URL (the port can change). The session carries over, since the hostname is the same. Check that the asset hash in the page's script URLs changed after a JS fix.
9. **Hand over a code fix.** Tell the user it's ready to review in the repro's worktree, with a short summary of the diff (`git -C <worktree> diff --stat`). Then offer:
   - to open it for review, e.g. `code <worktree>`;
   - when the run printed a `Source worktree:`, to apply the fix there. Only with a yes: check `git -C <source> status` first and mention any uncommitted changes it already has, then run `git -C <worktree> diff | git -C <source> apply`. If that doesn't apply cleanly (the source moved on since the repro started), retry with `git apply --3way` and report any conflicts rather than resolving them yourself.

   The fix is left uncommitted either way. Never commit or push it.
10. **Keep the repro** unless the user says the investigation is over: the worktree holds the fix and the database holds the setup, so a follow-up question reuses both instantly. Once they're done: `git -C <project-repo> worktree remove --force <worktree>` and `dropdb odoo-repro-<name>`. The worktree lives in the job tmp dir, so it also goes when the job ends.

## Common pitfalls

- **No section for the branch's version.** A run against an 18.0 branch with only `[19.0]` configured stops with an error. Ask the user for the 18.0 paths and add a section; don't force it through `--odoo-version=19.0`.
- **Modules missing.** A new DB gets only `base`. Pass `--modules=` with the module that owns the failing model or screen, plus the app whose menu the user named (e.g. `stock` for Inventory). On a later run, any listed module the DB lacks is installed first, so `--serve` doesn't need `--modules` repeated.
- **No demo data.** Databases are created without it, so there are no sample products, partners or POS configs. Create the records the bug needs in the setup script.
- **A DB outliving its worktree.** Job tmp dirs are deleted with the job, but the databases are not. A new job with the same `--name` gets a fresh worktree next to the old database; `--fresh` rebuilds the database when that mismatch matters.
- **Stale `git worktree list` entries** after a job tmp dir is deleted out from under git. `git -C <project-repo> worktree prune` clears them.
- **`git worktree` needs the project repo.** Run worktree commands with `-C <project-repo>` (one of the `projects` from the setup check), not from a parent folder that holds several checkouts.
- **An open POS reloads from its local cache.** Reloading `/pos/ui` for a session that's already open serves the data the browser stored earlier, so a data, setting or Python fix seems to have no effect. Reopen it from the backend instead: `/pos/ui/<config_id>?from_backend=True`.
- **Forgetting `env.cr.commit()`** after `open_ui()` / state-changing setup — subsequent reads (and HTTP requests) run in a fresh transaction and won't see uncommitted state.
- **Stdout buffering** can reorder parent prints behind subprocess output. Confusing but not a correctness issue.
- **Chrome navigation blocked**: the Claude in Chrome extension may need permission for `*.localhost`. Approve when prompted.
