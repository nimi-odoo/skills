# skills

Claude Code skills for working on Odoo projects locally.

| Skill | What it does |
|---|---|
| `/setup-nimi-skills` | Finds the local Odoo checkouts, Enterprise checkouts, venvs and project repos, confirms them, and writes the workspace conf the other skills read. Run it once before the other skills. |
| `/reprodoo` | Reproduces an Odoo bug against a branch's exact code, in `odoo-bin shell` for Python errors or a local server plus Chrome for UI errors. See [its README](skills/reprodoo/README.md). |

## Install

The skills can be installed in two ways. Only one is needed; installing both makes every skill appear twice.

### Plugin

Managed by Claude Code and updated with `/plugin update`.

```
/plugin marketplace add nimi-odoo/skills
/plugin install nimi-skills@nimi
```

- Installed and updated from inside Claude Code, with no clone or shell script
- Skills are namespaced (`/nimi-skills:reprodoo`), so they never clash with existing skills of the same name
- Only released versions are installed

### Clone and link

Plain files, updated with `git pull`.

```bash
git clone https://github.com/nimi-odoo/skills.git
cd skills
./link-skills.sh
```

- Updates are a `git pull`
- Skills keep their short names (`/reprodoo`)

`link-skills.sh` symlinks each skill into `~/.claude/skills`. A skill whose name is already taken there is skipped, never replaced. `./link-skills.sh --unlink` removes the links.

### Then

Run `/setup-nimi-skills`. It asks where the Odoo checkouts are kept (a folder is enough; it finds the rest), presents what it found, and writes only after confirmation:
- the conf, at a chosen location
- a short `## nimi-skills` section in `~/.claude/CLAUDE.md` recording where the conf is

## Requirements

- Python 3.10+ as `python3` (the scripts use only the standard library)
- PostgreSQL client tools (`psql`, `dropdb`) that reach the server with no extra flags, via `PGHOST`/`PGUSER` in the shell if needed
- Chrome, for browser repros and JS tests
- Browser repros use the Claude in Chrome extension

## The workspace conf

One section per Odoo version; see [`odoo-workspace.conf.example`](odoo-workspace.conf.example). Each version needs its own Odoo and Enterprise checkout, as a separate clone or a git worktree. Scripts take the conf as `--conf=PATH` or from `$ODOO_WORKSPACE_CONF`.

Each run picks a version and prints which one and why, e.g. `Odoo 19.0 (__manifest__ versions)`:

1. `--odoo-version=19.0`, if passed
2. The section whose `community` or `enterprise` checkout contains the target
3. The target's `__manifest__.py` versions (`19.0.1.2.0` → `[19.0]`) and branch name (`19.0-fix-foo`, `saas-19.1-bar`)
4. The only section, when just one is configured

Otherwise the run stops and lists what it found instead of guessing: no version in either place, modules declaring several versions, manifests and branch name disagreeing, or a version the conf has no section for.

## Scripts

Shared by the skills, in `scripts/`. They also work by hand.

| Script | Use |
|---|---|
| `repro.py` | Run a bug script in `odoo-bin shell`, or serve HTTP for browser repros |
| `run_tests.py` | `run_tests.py MODULE[,MODULE...]\|all [Test.method] [--python\|--js] [--fresh] [--custom-root=PATH]`: Python and JS tests for any number of modules in one run, or one suite with `--python` / `--js`. JS tests use Hoot, which arrived in Odoo 18.0; on earlier versions only the Python tests run, and the output says the JS tests were skipped |
| `odoo_workspace.py` | The conf, version picking, database naming, and `find` for setup |

## Databases

Both scripts reuse their databases between runs:

| Name | Made by | Holds |
|---|---|---|
| `odoo-repro-<name>` | `repro.py` | One bug's setup. `<name>` is a short slug for what's being reproduced, picked by the skill |
| `odoo-tests-<repo>-<branch>` | `run_tests.py` | Every module tested on that branch so far; new ones are installed on the run that asks for them |

Each database is tagged with the repo and branch it was built from. When a name is already taken by a database built from other code, the script uses `<name>-2`, `-3`, … and says so. Names are cut to Postgres' 63-byte limit, ending in a short hash when cut.

To reset databases:
- `--fresh`, on either script, drops the database for that run and builds it again, e.g. after a branch removes a module.
- `run_tests.py --cleanup` drops every `odoo-tests-*` database at once. Repro databases are dropped one at a time with `dropdb odoo-repro-<name>`.

## Ports

| Server | Default port | Changed with |
|---|---|---|
| An Odoo started by hand for development | 8069 | |
| `run_tests.py` | 8169 | `ODOO_TEST_PORT` |
| `repro.py --serve` | 8269 | `--port` |

The defaults differ so all three can run at the same time. When a script's port is already taken, for example by a second repro, it uses the next free one and prints which.
