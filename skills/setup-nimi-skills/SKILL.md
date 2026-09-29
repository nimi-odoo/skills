---
name: setup-nimi-skills
description: "Set up this machine's Odoo workspace for the nimi skills: find the Odoo checkouts, Enterprise checkouts, venvs and project repos, confirm them with the user, and write the workspace conf the other skills read. Run once before first use, and again when adding an Odoo version or project."
disable-model-invocation: true
---

# Set up nimi skills

The other skills in this repo (reprodoo, and more to come) run Odoo locally. They need to know, per Odoo version, where the Community checkout, Enterprise checkout, Python venv and project repos are. This skill finds those, confirms them with the user, and writes them to one workspace conf.

The conf describes the whole machine, not one repo. So the user decides where to look. Don't search their disk before they say where.

This skill reads the user's checkouts and writes exactly two things: the conf, and one section of `~/.claude/CLAUDE.md`. Every choice it offers is about what the conf includes. Word it that way: a repo is *included under [18.0]* or *disincluded from the conf file*, and disincluding it only means the other skills need `--repo=<path>` for it.

The user is often being onboarded and doesn't yet know the skill never changes their environment. So never describe a conf choice with a verb that sounds like it acts on their machine: no "drop", "remove", "delete", "prune", "clean up", "discard" or "skip" for versions, checkouts, repos, worktrees, venvs or databases. Say "disinclude from the conf file", even when it reads longer. That wording is enough; don't add reassurances that nothing on disk changes.

The discovery and conf code lives in the scripts shared by this repo's skills:

```bash
$(realpath "${CLAUDE_SKILL_DIR}/../../scripts")
```

Below, `$SCRIPTS` stands for that path. Write it out in each command, since shell variables don't carry over between commands.

## Process

### 1. Check what's already there

- Look for a `## nimi-skills` section in the user's `~/.claude/CLAUDE.md`. It holds the path of an existing conf.
- Check `$ODOO_WORKSPACE_CONF`.
- If a conf exists, say that this machine is already set up, show what the conf holds (`python3 $SCRIPTS/odoo_workspace.py show --conf=<path>`, plus the file itself), and ask which of these the user wants:
  - **Reconfigure from scratch:** run every step below as if nothing existed, and let the new conf replace the old one only when the user confirms in step 6.
  - **Change something:** add a version, add projects, or fix a path. Update the existing conf rather than starting over, and skip the steps that don't apply.
  - **Leave it:** stop here.
- If none exists, this is a first-time setup: carry straight on, and open with only the result of the basics check below (e.g. "Python 3.11 and Postgres are ready.").
- Check the basics the scripts need: `python3 --version` is 3.10 or newer, and `psql -l` reaches Postgres. If `psql` fails, tell the user (usually `PGHOST`/`PGUSER` in their shell) and continue.

### 2. Ask where to look

Ask one question. Lead with the recommended answer:

> Where do you keep your Odoo checkouts? Give me a folder (e.g. `~/odoo`, `~/dev`) and I'll find everything under it. I can search your whole home folder instead, which is slower. If you'd rather give exact paths, that works too.

Take whichever they give. Exact paths skip discovery for that item. Several folders are fine.

### 3. Discover

For each folder:

```bash
python3 $SCRIPTS/odoo_workspace.py find <folder>
```

It prints JSON with five lists. Every community, enterprise, venv and project entry carries `version_folder`: the nearest folder at or above it named for a series (`18`, `18.0`, `v18`, `odoo18`), else null.
- `community`: checkouts with `odoo-bin`, and the version from `odoo/release.py`
- `enterprise`: checkouts containing `web_enterprise`, the series their branch names (`branch_version`) and `location_version`
- `venvs`: virtualenvs, and whether they can import Odoo's dependencies (`has_odoo_deps`)
- `projects`: git repos with Odoo modules (each worktree is its own entry), with:
  - `main_checkout`: the repo's main worktree; entries sharing it are one repo
  - `remote`: `origin`'s URL; two main checkouts sharing it are copies of one repo
  - `manifest_versions`: series declared by module versions like `18.0.1.0.0`, with counts
  - `branch_versions`: series led by every local and remote branch name, with counts
  - `location_version`: the series of the nearest folder above it named for one (`18`, `18.0`, `v18`, `odoo18`), else the one series of the nearest checkouts of known series sharing a folder with it (community checkouts, enterprise branches, projects whose manifests declare one series). `~/odoo/17/client-a` is 17.0, and so is `~/dev/sandbox/17/enterprise` even when `~/dev/sandbox` holds a 16.0 checkout.
- `version_folders`: one entry per version folder, with what it holds and the evidence for telling a real workspace from a stray copy:
  - `community`, `enterprise`, `venvs`, `projects`: the paths found in it
  - `runnable`: it holds a community checkout and a venv with `has_odoo_deps`
  - `siblings`: the other series whose runnable folders share its parent. `~/odoo/18` beside runnable `~/odoo/17` and `~/odoo/19` has `["17.0", "19.0"]`
  - `last_activity`: the last fetch or HEAD move of any checkout in it

The branch a checkout happens to be on is arbitrary, so it never decides a project's version.

### 4. Propose one section per version

Group what was found by Odoo version:
- **community**: the checkout for that version.
- **enterprise**: the one next to the community checkout (`location_version`), else the one whose branch names the version. None is fine (community-only).
- **python**: a venv with `has_odoo_deps` next to that checkout or named for the version (`venv19`). If none qualifies, say so; don't pick one that can't import Odoo.
- **projects**: how repos are placed depends on the layout. Don't assume either one.
  - **Version folders** (each version's checkouts and repos under one folder, like `~/odoo/16/`): the version's projects path is the folder inside the winning version folder that holds its project repos (e.g. `~/odoo/16/clients`), including the folders under it.
  - **No version folders** (a flat layout like `~/dev/odoo`, `~/dev/enterprise`, `~/dev/client-a`): there is no projects path. Place every repo by its evidence (rule 2) and ask about all of them in one multiSelect question per version. That's the normal flow for this layout, not a list of exceptions, so present it as the version's proposed repos.
  1. Include every repo in the projects path. Where the user keeps a repo is where they work on it: a repo in `~/odoo/15/clients/` is included under [15.0] even when most of its branches name 14.0. Don't ask about it.
  2. Flag every repo outside it, and leave it out until approved. That covers repos whose `location_version` only comes from nearby checkouts (`~/dev/docker/addons/foo` next to a 16.0 checkout), repos in a losing version folder, and repos with no version folder. Put each one under the version its evidence points to: `location_version`, then `manifest_versions`, then the series most of `branch_versions` lead with. An upgraded repo keeps branches for older series and may already have some for the next one, so weigh the counts.
  3. Add a flagged repo to a section only when the user approves it. It then becomes another projects path for that version.
  4. Include worktrees of one repo once, by `main_checkout`: the skills find the rest through git. A worktree outside the projects path whose `main_checkout` is inside it is not flagged.

**Duplicates.** A version can have more than one candidate: several version folders for one series, several community or enterprise checkouts of it, several venvs, or copies of a project (main checkouts sharing a `remote`). Don't assume any particular layout. Pick by `version_folders`, strongest signal first:
1. `siblings`: a folder that sits with the user's other working versions is where they work. One of five version folders side by side beats one on its own.
2. `runnable`: a folder the skills can run from beats one missing its community checkout or a venv.
3. Completeness: community, enterprise, venv and projects together beat a partial set.
4. Use: more projects and worktrees, and a more recent `last_activity`.

A candidate with no `version_folder` ranks below one in a runnable folder with siblings. Take each path in the version's section from the winning folder. Only take a community, enterprise or python path from elsewhere when the winner leaves that slot empty. Projects never come from elsewhere without the user's approval.

This ranking gives a recommendation, not a decision. Nothing in the proposal is final until the user confirms it, and that includes duplicates. When one folder clearly wins, stage it in the version's table as the recommendation and flag the others as duplicates. Don't ask about it upfront; the user resolves it through the last question if they disagree. Never describe a duplicate as resolved, decided or already left out. When two are close (both runnable with siblings, or neither), there is no obvious recommendation: ask an AskUserQuestion question for that version, one option per folder with its evidence in the description, and your pick first.

**Present.** Show the tables in the chat text, before any question, so the user can check the whole proposal at a glance. Never move them into an AskUserQuestion preview. The dialog is too small and drops information.
- One table per version, each path filled in. Mark every cell that has duplicates as staged, e.g. `~/odoo/18/venv18` *(staged, see ⚑2)*.
- Under it, a **Flagged duplicates** list, numbered so the user can refer to them. One line each: the staged pick, the duplicate, and the evidence (e.g. "⚑2 `~/odoo/18/venv18` staged over `~/odoo/18/venv-test`: same folder, `venv18` is named for the version"; "⚑3 `~/odoo/16` staged over `~/odoo/archive/16` (enterprise + 2 repo copies): no siblings, not runnable, last active a year ago").

**Ask.** Collect every decision with AskUserQuestion, so the user clicks options instead of typing a reply:
- **Flagged repos:** one multiSelect question per version that has any: "Include any of these under [16.0]?", one option per repo, its label the repo name and its description the evidence and why it sits outside the projects path (e.g. "no version folder, 16.0 only from a neighbouring checkout"). Unchecked repos are disincluded from the conf file.
- **Repos with no signal** (no `location_version`, manifests or series branches): one question per repo: "Include `client-tools` under which version?", options "Disinclude from the conf file" then up to three versions, most recently active first. The user types any other version through "Other".
- **Versions, paths and duplicates:** a last question, "Include every version as shown?", options "Yes, include all" (description: "Accept every staged pick"), "Swap a flagged duplicate" (only when there are any), "Disinclude some versions", "Fix a path". Any answer but yes gets a follow-up question for it, then the updated tables and this question again, until the user says yes.
  - "Swap a flagged duplicate": one question per flagged duplicate the user names, or a multiSelect "Which flagged duplicates do you want to change?" first when there are several. Options: the staged pick first, marked "(Recommended)", then each duplicate, each with its evidence in the description.

AskUserQuestion takes up to 4 questions per call and 4 options per question; split larger sets across calls.

### 5. Ask where to save the conf

> Where should I save the conf? It holds this machine's paths, so keep it out of any repo you share.

Suggest `odoo-workspace.conf` in the closest folder that holds all the checkouts they confirmed in step 4. For example, checkouts at `~/odoo/18/` and `~/odoo/19/` give `~/odoo/odoo-workspace.conf`. Let them choose.

### 6. Show the drafts, then write

Show both before writing anything:
- The conf, in the format of `odoo-workspace.conf.example` at the repo root (one `[<version>]` section each, `projects` comma-separated).
- The section for `~/.claude/CLAUDE.md`:

  ```markdown
  ## nimi-skills

  Odoo workspace conf: `<path>`. Pass it as `--conf=<path>` to the nimi skills' scripts.
  ```

Let the user edit either one. Write them once the user says yes to writing. A reply that asks something or comments on the drafts gets an answer and the drafts again. Then write the conf, and add the section to `~/.claude/CLAUDE.md`, or update it if it's already there. Leave everything else in that file alone.

### 7. Check it

For each version:

```bash
python3 $SCRIPTS/odoo_workspace.py show --conf=<path> --odoo-version=<version>
```

Report which versions resolved, and any `odoo-bin` or venv the script couldn't use. Then tell the user the skills are ready, e.g. `/reprodoo`.
