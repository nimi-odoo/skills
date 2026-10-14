---
name: spin-db
description: Serve a seeded local Odoo DB so the user can see the workflow being discussed running.
---

# Spin up a seeded Odoo database

Make the thing being discussed real and visible: install the modules it needs, seed the records it needs, and hand the user a URL and a walkthrough.

The plumbing is `spin.py`, one of the scripts shared by this repo's skills. They live at:

```bash
$(realpath "${CLAUDE_SKILL_DIR}/../../scripts")
```

Below, `$SCRIPTS` stands for that path. Write it out in each command, since shell variables don't carry over between commands.

## Setup check

Paths to Odoo, Enterprise, the venv and the project repos come from the user's workspace conf, one section per Odoo version. `/setup-nimi-skills` writes it and records its path in a `## nimi-skills` section of the user's CLAUDE.md. Pass that path as `--conf=<path>` on every script call, unless `$ODOO_WORKSPACE_CONF` is set.

If there's no such section and no `$ODOO_WORKSPACE_CONF`, stop and ask the user to run `/setup-nimi-skills`. Don't write a conf yourself.

## Inputs

Infer everything from the conversation:
- **Code** to serve (`<target>`): the worktree being worked on, as a path, or a branch name. Leave it out when the workflow is plain Community/Enterprise. If it's unclear which worktree, ask.
- **Version** (`--odoo-version`): the script reads it from the target's code. With no target, deduce it from the signals at hand (the cwd, branch names, paths and versions named in the conversation) and pass it. Ask only when the signals disagree or none points to one.
- **Modules**: what the workflow needs, plus the obvious supporting ones (a language for a translation, `pos_loyalty` for rewards). Ask only when it's genuinely ambiguous.
- **Name**: a short slug for the workflow, like `pos-refund` or `portal-invoice`. It names the database `odoo-spin-<name>` and its URL.
- **Data**: the records the workflow needs. You create them, never the user. A loose description from the user ("a few products, one with a variant") is enough; step 2 turns it into a plan.

State what you're spinning up (code, modules, name) in one sentence and carry on. Don't wait for approval on those unless something is ambiguous; the data goes through step 2.

## 1. Start the server

Start it with `run_in_background`, sending its output to a log:

```bash
python3 $SCRIPTS/spin.py --conf=<path> start [<target>] --name=<slug> --modules=<a,b> \
    [--update=<a,b>] [--odoo-version=V] [--repo=PATH] [--fresh] > $CLAUDE_JOB_DIR/tmp/spin_<slug>.log 2>&1
```

Wait for it to end on `>>> READY` or `>>> FAILED`:

```bash
until grep -qE ">>> (READY|FAILED)" $CLAUDE_JOB_DIR/tmp/spin_<slug>.log; do sleep 1; done; grep -v "^20..-" $CLAUDE_JOB_DIR/tmp/spin_<slug>.log
```

- `Name:` is the name to use from now on. A name already used for other code moves on to `<slug>-2`.
- `>>> READY: <url>` gives the URL.
- Login is `admin` / `admin`.

## 2. Seed the data

Seeding runs a **generator**: a Python file run in `odoo-bin shell` against the spin's database, with `env` as a global. The server can stay up while it runs.

```bash
python3 $SCRIPTS/spin.py --conf=<path> seed --name=<name> <generator.py> [key=value ...]
```

Find one before writing one. Before running a supplied or saved one, say in one line which one and with what arguments (e.g. "catalog.py, count=50"), then run it unless the user objects.

1. **The user supplied one** (a path, or "use my catalog script"): run it. A CSV or JSON file gets a small generator that loads it.
2. **A saved generator fits**: `python3 $SCRIPTS/spin.py --conf=<path> generators` lists those in the conf's `generators` folders, one line each. Read the file for its arguments, and pass what the workflow needs as `key=value`. Only use one listed under the spin's version, since fields change between versions.
3. **Otherwise, suggest the data.** Take whatever the user said, however loose ("some customers, one with a credit limit"), and fill in the rest from the conversation, the modules and the flow being shown. Aim for data that makes the workflow easy to see: realistic names, the edge case under discussion, just enough records to show the behaviour.

   Propose it as a short list: which records, how many, their key values. Mark what you inferred as assumptions, e.g. *(assumed: prices tax-included, since the flow is POS)*. The user replies "yes" or corrects a line; don't make them spell out the data. Use AskUserQuestion only for a real fork, e.g. volume (a few / realistic set / load-test size), with your suggestion first. Ask while the server installs in the background, so it costs no time.

   A trivial setup (one or two records the flow obviously needs) skips the proposal: say what you'll create and go.

   Once the user confirms and the install is done, look at what the database already holds for the flow (shops, payment methods, journals, warehouses), then write the generator in `$CLAUDE_JOB_DIR/tmp/gen_<what>.py`:

```python
"""A POS shop, a cashier and N products available in the POS.

Args: count (int, default 20), cashier (str, default 'cashier')
"""
from spin import seed_args
from repro import as_user

args = seed_args(count=20, cashier='cashier')
if not env['pos.config'].search([], limit=1):
    env['pos.config'].load_onboarding_retail_scenario()  # a shop with Cash and Card payment methods
user = as_user(env, args['cashier'], ['base.group_user', 'point_of_sale.group_pos_user'])  # password = login
Product = env['product.product']
for i in range(1, args['count'] + 1):
    name = f"Product {i}"
    if not Product.search([('name', '=', name)], limit=1):  # safe to run twice
        Product.create({'name': name, 'list_price': 10 * i, 'available_in_pos': True})
env.cr.commit()  # shell changes are rolled back without it
print(f"{args['count']} products, cashier {user.login}")
```

A generator:
- starts with a docstring whose first line says what it creates, then an `Args:` line; `spin.py generators` shows the first line
- uses the ORM, so records behave as if a user made them. Use `psql` only for bulk inserts where speed matters more.

Settings (system parameters in particular) are cached by the running server; after a generator changes them, restart it (step 4).

### Offer to save it

Offer to save a generator you wrote once it has worked, but only when it creates more than a handful of records or covers a flow the user will test repeatedly (a catalog, a company setup). Ask where with AskUserQuestion, recommended first: a folder already in the conf's `generators` for this version; a new `generators/<version>/` next to the conf; or a folder in the project repo when the team should share it (committing stays with the user). A new folder gets listed only once the conf's `[<version>]` section has a `generators = <folder>` line; add it after the user says yes. Keep saved generators general: arguments for what varies, no values that only fit this conversation.

## 3. Hand over

The URL and a walkthrough, scaled to the flow. A simple one gets the URL and a line; a new or multi-step one gets numbered steps with the exact records to use:

```
Server ready: http://odoo-spin-pos-refund.localhost:8369/web (admin / admin)

1. Point of Sale > Shop > Open Register
2. Sell "Product 3" ($30), pay cash
3. Orders > the order > Refund
4. Check the refund line shows -$30
```

Assume the user knows Odoo; don't explain standard navigation. Use the URL's hostname, not plain `localhost`: each spin has its own host so its login cookie doesn't clash with other servers.

## 4. Code changes → restart

When you edit the served worktree during the conversation, keep the server current without asking, and tell the user when it's back:
- **XML templates and views**: `--dev=xml` reloads QWeb templates. New or changed views, fields, data files or JS need an update.
- **Python**: `--dev=reload` restarts on edits only when `watchdog` is installed in the venv. The log says `'watchdog' module not installed` when it isn't; then restart.

To restart, with an update of the edited modules:

```bash
python3 $SCRIPTS/spin.py stop --name=<name>
python3 $SCRIPTS/spin.py --conf=<path> start [<target>] --name=<name> --update=<edited modules> > $CLAUDE_JOB_DIR/tmp/spin_<slug>.log 2>&1   # run_in_background
```

Then wait for READY as in step 1 and take the port from it again.

## 5. Clean up

Offer to clean up when the user is done with it:

```bash
python3 $SCRIPTS/spin.py --conf=<path> cleanup --name=<name>
```

## Common pitfalls

- **No demo data.** Databases are created without it, so every record the walkthrough mentions comes from a generator. That includes the setup records demo data normally provides: a POS install has no shop and no payment method, for instance.
- **Wrong worktree.** A branch name serves the worktree that has it checked out, uncommitted changes included. For the code as deployed, use `/reprodoo` instead.
- **An open POS reloads from its local cache.** After a data or code change, reopen it from the backend (`/pos/ui/<config_id>?from_backend=True`) instead of reloading `/pos/ui`.
- **Chrome navigation blocked** when driving the browser yourself: the Claude in Chrome extension may need permission for `*.localhost`.
