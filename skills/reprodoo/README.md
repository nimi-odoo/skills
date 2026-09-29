# reprodoo

Reproduces an Odoo bug locally against a branch's exact code, finds the cause, and checks the fix. Python errors (tracebacks, access errors, RPC errors) are reproduced in `odoo-bin shell`; browser errors and wrong UI behaviour in a local server driven through Chrome. The cause can be code or configuration.

Nothing runs against the staging server, and the code the repro starts from is never edited.

## Before the first run

- Run `/setup-nimi-skills` once, so the skill knows where the Odoo checkouts, venvs and project repos are.
- For browser bugs, install the Claude in Chrome extension.

## Usage

```
/reprodoo <what's broken>
```

The report works best with four things: the **branch**, **who** hits it, **what they do**, and **what goes wrong**. Error text and screenshots help.

```
/reprodoo <staging-branch-experiencing-issue>: an accountant posting some vendor bills
gets a server error, "KeyError: 12". Other bills post fine.
```

Missing details are asked for in a single question. Everything else, from creating users and records to setting options and clicking through the flow, is done by the skill.

## What a run creates

| What | Where | Notes |
|---|---|---|
| A worktree | `<version>-repro-<name>` in the job's temp folder | Starts from `origin/<branch>`, or from a given worktree's commit or any `--ref`. Git submodules are filled in. Removed with the job's temp folder |
| A database | `odoo-repro-<name>` | Built with only the modules the bug needs and no demo data. Kept after the job ends |
| A local server (browser bugs) | `http://odoo-repro-<name>.localhost:<port>` | A hostname of its own, so several repros can run in one browser at once |

`<name>` is a short slug for the bug, like `vendor-bill-post-keyerror`, chosen from the report. The worktree and database are reused while the same bug is investigated, so follow-up questions are fast.

## When it finds the cause

- **Code:** the fix is made in the repro's worktree and checked by re-running the repro. The skill then offers to open that worktree for review, and to apply the fix, uncommitted, to the worktree the repro started from. Nothing is ever committed or pushed.
- **Configuration:** the triggering setting and value are named, the fix is checked by changing the setting in the repro database, and the report says whether the code should also handle that setting.

## Cleaning up

The worktree goes when the job's temp folder is cleared. Databases stay until dropped:

```bash
dropdb odoo-repro-<name>
```
