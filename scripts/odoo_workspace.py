#!/usr/bin/env python3
"""
The local Odoo workspace shared by the skills' scripts: which checkouts, venvs
and project repos exist, which one a run should use, and the databases built
from them.

The conf is the file passed as --conf, else $ODOO_WORKSPACE_CONF. /setup-nimi-skills
writes it and records its path in CLAUDE.md. It holds one section per Odoo
version (see odoo-workspace.conf.example); relative paths resolve against the conf's folder.

The version for a run is picked in this order:
  1. --odoo-version
  2. the section whose community or enterprise checkout contains the target (e.g. its community/addons)
  3. the target's manifests (18.0.1.2.0 -> [18.0]) and branch name (18.0-fix-foo -> [18.0]),
     which must agree when both say something
  4. the only section, when just one is configured (or, with no target, the only one with projects)
Anything else stops the run and asks for --odoo-version rather than guess.

CLI:
    odoo_workspace.py show [--conf=PATH] [--target=PATH] [--odoo-version=V]   resolved paths
    odoo_workspace.py find DIR                                               Odoo checkouts, venvs and
                                                                             project repos under DIR, as JSON
"""

import argparse
import ast
import configparser
import functools
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

MAX_DB_NAME = 63  # Postgres identifier limit
LOG_DIR = Path(os.environ.get('ODOO_DEV_LOG_DIR', '/tmp'))
# A full module version is the series plus 2-3 parts; shorter ones get the server series prefixed.
FULL_VERSION_RE = re.compile(r'^((?:saas~)?\d+\.\d+)(?:\.\d+){2,3}$')
# Odoo.sh-style branch names lead with the series: 19.0, 19.0-fix-foo, saas-18.3-bar
BRANCH_VERSION_RE = re.compile(r'^(saas-)?(\d+\.\d+)(?:$|[-_.])')
RELEASE_VERSION_RE = re.compile(r"version_info\s*=\s*\(\s*'?(saas~)?(\d+)'?\s*,\s*(\d+)")


# ─── Addons dirs and git ─────────────────────────────────────────────────────

def _manifests(addons_dir):
    """(module name, manifest dict) for each module directly under addons_dir."""
    for path in sorted(Path(addons_dir).glob('*/__manifest__.py')):
        try:
            manifest = ast.literal_eval(path.read_text())
        except (SyntaxError, ValueError, UnicodeDecodeError):
            continue
        if isinstance(manifest, dict):
            yield path.parent.name, manifest


def manifest_versions(addons_dir):
    """Series declared by each module directly under addons_dir, as a Counter."""
    found = Counter()
    for _name, manifest in _manifests(addons_dir):
        match = FULL_VERSION_RE.match(str(manifest.get('version', '')))
        if match:
            found[match.group(1)] += 1
    return found


def installable_modules(addons_dir):
    return [name for name, manifest in _manifests(addons_dir) if manifest.get('installable', True)]


def _asset_globs(entries):
    """File globs an assets list adds: plain paths, and the path of (after|before|prepend|append|replace, ..., path)."""
    for entry in entries:
        if isinstance(entry, str):
            yield entry
        elif isinstance(entry, (list, tuple)) and entry and entry[0] not in ('include', 'remove'):
            yield entry[-1]


def hoot_modules(addons_dir, modules):
    """Those of modules that add at least one .test.js file to web.assets_unit_tests, the bundle Hoot loads.

    Asset paths are relative to the addons dir, like the addons path entry Odoo resolves them against.
    """
    addons_dir = Path(addons_dir)
    with_tests = set()
    for name, manifest in _manifests(addons_dir):
        entries = (manifest.get('assets') or {}).get('web.assets_unit_tests', [])
        if any(match.name.endswith('.test.js') for pattern in _asset_globs(entries)
               for match in addons_dir.glob(pattern)):
            with_tests.add(name)
    return [m for m in modules if m in with_tests]


def branch_version(branch):
    """Series a branch name leads with, ignoring any remote prefix, else None."""
    for name in (branch, branch.partition('/')[2]):
        match = BRANCH_VERSION_RE.match(name)
        if match:
            return f"saas~{match.group(2)}" if match.group(1) else match.group(2)
    return None


def _git_out(path, *cmd):
    try:
        return subprocess.check_output(
            ['git', '-C', str(path), *cmd], text=True, stderr=subprocess.DEVNULL,
        ).strip() or None
    except (subprocess.CalledProcessError, FileNotFoundError, NotADirectoryError):
        return None


def current_branch(worktree):
    return _git_out(worktree, 'symbolic-ref', '--short', '-q', 'HEAD')


def main_checkout(path):
    """The main worktree of the repo that path belongs to, else None."""
    out = _git_out(path, 'worktree', 'list', '--porcelain')
    return Path(out.splitlines()[0].removeprefix('worktree ')) if out else None


def checkout_ref(path):
    """(repo, branch) for a checkout; a detached one uses its folder name as the branch."""
    path = Path(path).expanduser().resolve()
    return main_checkout(path), current_branch(path) or path.name


def submodules(worktree):
    """(path, initialized) for each git submodule of worktree, nested ones included."""
    out = _git_out(worktree, 'submodule', 'status', '--recursive') or ''
    # Lines are "<flag><sha> <path> (<describe>)"; a leading "-" means not initialized.
    return [(Path(worktree) / line[1:].split()[1], line[0] != '-') for line in out.splitlines() if line.strip()]


def init_submodules(worktree):
    """Check out every submodule of worktree at the commit its branch pins."""
    subprocess.run(['git', '-C', str(worktree), 'submodule', 'update', '--init', '--recursive'], check=True)


@functools.cache
def addons_dirs(root):
    """root, plus each initialized submodule dir holding modules, as Odoo.sh puts them on the addons path.

    Warns about submodules that were never initialized, since modules depending on theirs won't install.
    """
    root = Path(root).resolve()
    dirs = [root]
    empty = []
    for path, initialized in submodules(root):
        if not initialized:
            empty.append(path.relative_to(root))
        elif next(path.glob('*/__manifest__.py'), None):
            dirs.append(path)
    if empty:
        print(f"WARNING: submodules not initialized in {root}: {', '.join(map(str, empty))}. Modules that "
              f"depend on theirs won't install; `git -C {root} submodule update --init --recursive` fills them.",
              file=sys.stderr, flush=True)
    return tuple(dirs)


# ─── Databases ───────────────────────────────────────────────────────────────

def _slug(text):
    return re.sub(r'[^A-Za-z0-9._-]+', '-', text).strip('-')


def db_name(prefix, label, seq=1):
    """Stable DB name, cut to Postgres' limit with a hash so cut names stay distinct."""
    suffix = f"-{seq}" if 1 < seq else ''
    name = f"{prefix}-{_slug(label)}"
    if MAX_DB_NAME < len(name) + len(suffix):
        digest = hashlib.sha1(name.encode()).hexdigest()[:8]
        name = f"{name[:MAX_DB_NAME - len(suffix) - 9]}-{digest}"
    return name + suffix


def _literal(text):
    return "'" + text.replace("'", "''") + "'"


def _psql(sql, db='postgres'):
    return subprocess.check_output(['psql', '-tAqc', sql, db], text=True).strip()


def db_exists(name):
    return bool(_psql(f"SELECT 1 FROM pg_database WHERE datname = {_literal(name)}"))


def db_tag(name):
    return _psql(
        f"SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = {_literal(name)}"
    )


def set_db_tag(name, tag):
    ident = '"' + name.replace('"', '""') + '"'
    _psql(f"COMMENT ON DATABASE {ident} IS {_literal(tag)}")


def claim_db(prefix, label, tag):
    """The DB name for this run: the first of label, label-2, ... that is free or already carries tag.

    Returns (name, exists). A name taken by a DB with another tag belongs to different code.
    """
    seq = 1
    while True:
        name = db_name(prefix, label, seq)
        if not db_exists(name):
            return name, False
        if db_tag(name) == tag:
            return name, True
        print(f"{name} belongs to other code ({db_tag(name) or 'untagged'}), trying the next name", flush=True)
        seq += 1


def has_hoot(cfg):
    """Whether this Odoo runs Hoot unit tests (18.0+), whose suite can be filtered per module."""
    return (cfg.community / 'addons' / 'web' / 'static' / 'lib' / 'hoot').is_dir()


def _can_bind(addr, port):
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((addr, port))
        except OSError:
            return False
    return True


def free_port(start):
    """First port free from `start` upward, so parallel runs each get their own.

    Probes with SO_REUSEADDR, as Odoo binds, so a port a stopped server left in TIME_WAIT
    counts as free. Both addresses are probed because a server listening on 127.0.0.1 only
    doesn't block a wildcard bind, yet would still answer requests to localhost.
    """
    for port in range(start, start + 100):
        if all(_can_bind(addr, port) for addr in ('', '127.0.0.1')):
            return port
    sys.exit(f"No free port between {start} and {start + 99}")


def report_failure(run):
    """Call run(), printing a `>>> FAILED: <reason>` line on any error exit.

    A skill waiting on a background script's log stops at this line the way it stops at READY.
    """
    try:
        run()
    except SystemExit as e:
        if not e.code:
            raise
        print(f">>> FAILED: {e.code}", flush=True)
        sys.exit(1)
    except Exception as e:
        print(f">>> FAILED: {e!r}", flush=True)
        raise


# ─── Conf ────────────────────────────────────────────────────────────────────

@dataclass
class Config:
    version: str
    reason: str
    source: Path
    community: Path
    enterprise: Path | None
    python: str
    projects: list[Path]

    @property
    def odoo_bin(self):
        return self.community / 'odoo-bin'

    def addons_path(self, *more):
        """Community and Enterprise addons, then each dir in more with its submodules' addons dirs."""
        paths = [self.community / 'addons']
        if self.enterprise:
            paths.append(self.enterprise)
        for root in filter(None, more):
            paths += addons_dirs(Path(root))
        return ','.join(str(p) for p in paths)

    def odoo(self, db, addons, *args, command=None):
        """odoo-bin command line against db, with the addons dir being worked on added to the path.

        An empty config file keeps the user's ~/.odoorc (its addons, dbfilter, workers...) out of the run.
        """
        return [self.python, str(self.odoo_bin), *([command] if command else []), f'--config={os.devnull}',
                f'--addons-path={self.addons_path(addons)}', '-d', db, *args]

    def create_db(self, db, addons, modules, tag=None):
        """Create db with modules installed, keeping the install output in a log rather than the terminal."""
        print(f"Creating {db} with modules: {modules or 'base'}", flush=True)
        self.install(db, addons, modules or 'base')
        if tag:
            set_db_tag(db, tag)

    def install_missing(self, db, addons, modules):
        """Install those of the comma-separated modules that db doesn't have yet."""
        wanted = [m for m in modules.split(',') if m]
        installed = set(_psql(
            "SELECT name FROM ir_module_module WHERE state = 'installed'", db=db).splitlines())
        missing = [m for m in wanted if m not in installed]
        if missing:
            print(f"Installing into {db}: {','.join(missing)}", flush=True)
            self.install(db, addons, ','.join(missing))

    def install(self, db, addons, modules, flag='-i'):
        """Install modules into db (creating it if needed), keeping the output in a log rather than the terminal."""
        log = LOG_DIR / f'{db}-install.log'
        print(f"Install log: {log}", flush=True)
        with open(log, 'w') as out:
            result = subprocess.run(self.odoo(
                db, addons, flag, modules, '--stop-after-init', '--without-demo=all', '--no-http',
            ), stdout=out, stderr=subprocess.STDOUT)
        lines = log.read_text(errors='replace').splitlines()
        print(lines[-1] if lines else '(no output)', flush=True)
        if result.returncode:
            sys.exit(f"{'Update' if flag == '-u' else 'Install'} failed (exit {result.returncode}); see {log}")


def find_conf(path=None):
    path = path or os.environ.get('ODOO_WORKSPACE_CONF')
    if not path:
        sys.exit(
            "No Odoo workspace conf. Run /setup-nimi-skills to create one, "
            "or pass --conf=PATH / set ODOO_WORKSPACE_CONF."
        )
    conf = Path(path).expanduser()
    if not conf.is_file():
        sys.exit(f"Odoo workspace conf not found: {conf}. Run /setup-nimi-skills to create it.")
    return conf


class Conf:
    def __init__(self, path=None):
        self.source = find_conf(path)
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(self.source)
        except configparser.Error as e:
            sys.exit(f"Can't read {self.source}: {e}")
        self.sections = {name: dict(parser[name]) for name in parser.sections()}
        if not self.sections:
            sys.exit(f"{self.source} has no version sections, e.g. [19.0]")

    def _fail(self, problem):
        sys.exit(
            f"{problem}\n"
            f"Configured versions: {', '.join(self.sections)} ({self.source}).\n"
            "Pass --odoo-version=<version> to choose one."
        )

    def _list(self, section, key):
        return [self._path(p.strip()) for p in section.get(key, '').split(',') if p.strip()]

    def projects(self):
        """Distinct project repos across all sections."""
        return sorted({p for section in self.sections.values() for p in self._list(section, 'projects')})

    def generators(self):
        """(version, folder) for each folder of data generators in the conf, the `generators` key."""
        return [(name, p) for name, section in self.sections.items() for p in self._list(section, 'generators')]

    def _owning_sections(self, target):
        """Sections whose community or enterprise checkout contains target.

        The project repo is left out: its checkout can be on any version's branch, so manifests decide.
        """
        target = Path(target).expanduser().resolve()
        owners = []
        for name, section in self.sections.items():
            paths = [section.get('community', ''), section.get('enterprise', '')]
            if any(p.strip() and target.is_relative_to(self._path(p.strip())) for p in paths):
                owners.append(name)
        return owners

    def pick(self, version=None, target=None, branch=None):
        """The Config for this run; exits asking for --odoo-version when the choice is unclear.

        branch defaults to the branch checked out at target.
        """
        if version:
            if version not in self.sections:
                self._fail(f"No [{version}] section in the conf.")
            return self._config(version, '--odoo-version')

        owners = self._owning_sections(target) if target else []
        if len(owners) == 1:
            return self._config(owners[0], f"{target} is under [{owners[0]}] paths")

        found = manifest_versions(target) if target else Counter()
        if 1 < len(found):
            listed = ', '.join(f"{v} ({n} modules)" for v, n in found.most_common())
            self._fail(f"Modules in {target} declare several versions: {listed}.")
        branch = branch or (current_branch(target) if target else None)
        signals = {}
        if found:
            signals['__manifest__ versions'] = next(iter(found))
        if branch and branch_version(branch):
            signals[f"branch {branch}"] = branch_version(branch)
        if 1 < len(set(signals.values())):
            listed = ', '.join(f"{source} -> {v}" for source, v in signals.items())
            self._fail(f"The version signals for {target} disagree: {listed}.")
        if signals:
            detected = next(iter(signals.values()))
            if detected not in self.sections:
                self._fail(f"{target or branch} is {detected}, which the conf has no section for.")
            return self._config(detected, ' and '.join(signals))

        if len(self.sections) == 1:
            return self._config(next(iter(self.sections)), 'only version in the conf')
        # With no target, callers fall back to the section's project, so one with a project is unambiguous.
        with_projects = [name for name, section in self.sections.items() if section.get('projects')]
        if not target and len(with_projects) == 1:
            return self._config(with_projects[0], f"only [{with_projects[0]}] has projects")
        self._fail(f"Neither the manifests nor the branch name of {target or branch or 'the target'} give a version.")

    def _path(self, value):
        path = Path(value).expanduser()
        return path if path.is_absolute() else (self.source.parent / path).resolve()

    def _config(self, version, reason):
        section = self.sections[version]
        if not section.get('community'):
            sys.exit(f"[{version}] in {self.source} has no `community` path")
        community = self._path(section['community'])
        if not (community / 'odoo-bin').is_file():
            sys.exit(f"No odoo-bin in [{version}] community = {community} ({self.source})")
        python = section.get('python') or 'python3'
        if os.sep in python or python.startswith(('~', '.')):
            python = str(self._path(python))
        return Config(
            version=version,
            reason=reason,
            source=self.source,
            community=community,
            enterprise=self._path(section['enterprise']) if section.get('enterprise') else None,
            python=python,
            projects=self._list(section, 'projects'),
        )


# ─── Discovery, for /setup-nimi-skills ───────────────────────────────────────

SKIP_DIRS = {'node_modules', '__pycache__', 'site-packages', 'Library', 'Applications', 'Pictures', 'Music', 'Movies'}
MAX_DEPTH = 6
FOLDER_VERSION_RE = re.compile(r'^(?:v|odoo[-_]?)?(\d{2})(?:\.0)?$')


def _release_version(community):
    release = community / 'odoo' / 'release.py'
    match = RELEASE_VERSION_RE.search(release.read_text()) if release.is_file() else None
    if not match:
        return None
    saas, major, minor = match.groups()
    return f"saas~{major}.{minor}" if saas else f"{major}.{minor}"


def _venv_info(venv):
    python = venv / 'bin' / 'python'
    cfg = (venv / 'pyvenv.cfg').read_text()
    py_version = re.search(r'^version(?:_info)?\s*=\s*(\S+)', cfg, re.M)
    try:
        subprocess.run([str(python), '-c', 'import psycopg2, lxml, werkzeug'],
                       check=True, capture_output=True, timeout=20)
        has_deps = True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        has_deps = False
    return {'python': str(python), 'python_version': py_version and py_version.group(1), 'has_odoo_deps': has_deps}


def branch_versions(repo):
    """Series each local and remote branch name leads with, as a Counter.

    An upgraded repo keeps branches for its older series too, and one being upgraded already
    has some for the next, so this is a distribution to weigh, not an answer.
    """
    refs = _git_out(repo, 'for-each-ref', '--format=%(refname:short)', 'refs/heads', 'refs/remotes') or ''
    # Major series end in .0 (saas ones don't), which leaves out typos like 19.9-fix.
    return Counter(v for v in map(branch_version, refs.splitlines())
                   if v and (v.startswith('saas~') or v.endswith('.0')))


def folder_version(name):
    """Series a folder is named for (18, 18.0, v18, odoo18, odoo-18), else None."""
    match = FOLDER_VERSION_RE.match(name.lower())
    return match and f"{match.group(1)}.0"


def _location_version(path, anchors, root):
    """The series of the nearest folder named for one, or of the nearest anchors sharing a folder with path.

    Walks up from path's parent to root. At each folder, a version name decides first, so
    ~/dev/sandbox/18/enterprise is 18.0 even beside a 16.0 checkout in ~/dev/sandbox.
    Otherwise the first folder holding any anchor (checkout of known series) decides, and holds
    only for a single series: a repo beside an 18.0 enterprise checkout, or among project repos
    declaring 18.0, takes 18.0.
    """
    for folder in [path.parent, *path.parent.parents]:
        if folder_version(folder.name):
            return folder_version(folder.name)
        versions = {v for anchor, v in anchors if anchor != path and folder in anchor.parents}
        if versions:
            return versions.pop() if len(versions) == 1 else None
        if folder == root:
            return None
    return None


def _version_folder(path, root):
    """The nearest folder, path itself included, named for a series, up to root; else None."""
    for folder in [path, *path.parents]:
        if folder_version(folder.name):
            return folder
        if folder == root:
            return None
    return None


def _last_activity(checkout):
    """When checkout was last fetched or had HEAD move, as a POSIX time, else None."""
    times = []
    for name in ('FETCH_HEAD', 'logs/HEAD'):
        git_path = _git_out(checkout, 'rev-parse', '--path-format=absolute', '--git-path', name)
        if git_path and Path(git_path).is_file():
            times.append(Path(git_path).stat().st_mtime)
    return max(times, default=None)


def version_folders(found):
    """Evidence for each folder named for a series, to tell a real workspace from a stray copy.

    A folder is runnable when it holds a community checkout and a venv that can import Odoo's
    dependencies. siblings are the other series whose runnable folders share its parent:
    ~/odoo/18 beside ~/odoo/17 and ~/odoo/19 is one family of workspaces.
    """
    folders = {}
    for kind in ('community', 'enterprise', 'venvs', 'projects'):
        for item in found[kind]:
            if not item['version_folder']:
                continue
            folder = folders.setdefault(item['version_folder'], {
                'path': item['version_folder'], 'version': folder_version(Path(item['version_folder']).name),
                'community': [], 'enterprise': [], 'venvs': [], 'projects': [], 'last_activity': None,
            })
            folder[kind].append(item['path'])
            if kind != 'venvs':
                activity = _last_activity(item['path'])
                folder['last_activity'] = max(filter(None, [folder['last_activity'], activity]), default=None)
    deps = {v['path'] for v in found['venvs'] if v['has_odoo_deps']}
    for folder in folders.values():
        folder['runnable'] = bool(folder['community']) and bool(deps.intersection(folder['venvs']))
    for folder in folders.values():
        parent = Path(folder['path']).parent
        folder['siblings'] = sorted({other['version'] for other in folders.values()
                                     if Path(other['path']).parent == parent and other['runnable']
                                     and other['version'] != folder['version']})
        if folder['last_activity']:
            folder['last_activity'] = date.fromtimestamp(folder['last_activity']).isoformat()
    return list(folders.values())


def find(root):
    """Odoo checkouts, enterprise checkouts, venvs and project repos under root, and the version folders holding them."""
    root = Path(root).expanduser().resolve()
    found = {'community': [], 'enterprise': [], 'venvs': [], 'projects': []}
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        depth = len(here.relative_to(root).parts)
        dirnames[:] = [] if MAX_DEPTH <= depth else sorted(
            d for d in dirnames if not d.startswith('.') and d not in SKIP_DIRS
        )
        if 'pyvenv.cfg' in filenames:
            found['venvs'].append({'path': str(here), **_venv_info(here)})
            dirnames[:] = []
        elif 'odoo-bin' in filenames:
            found['community'].append({'path': str(here), 'version': _release_version(here),
                                       'branch': current_branch(here)})
            dirnames[:] = []
        elif (here / 'web_enterprise' / '__manifest__.py').is_file():
            branch = current_branch(here)
            found['enterprise'].append({'path': str(here), 'branch': branch,
                                        'branch_version': branch and branch_version(branch)})
            dirnames[:] = []
        elif (here / '.git').exists() and next(here.glob('*/__manifest__.py'), None):
            main = main_checkout(here)
            found['projects'].append({
                'path': str(here),
                'main_checkout': str(main) if main else None,
                'remote': _git_out(here, 'remote', 'get-url', 'origin'),
                'manifest_versions': dict(manifest_versions(here)),
                'branch_versions': dict(branch_versions(here)),
                'worktrees': (_git_out(here, 'worktree', 'list') or '').count('\n') + 1,
            })
            dirnames[:] = []

    # Anchors: a community checkout's release.py, an enterprise checkout's series branch,
    # and a project whose manifests all declare one series.
    anchors = [(Path(c['path']), c['version']) for c in found['community'] if c['version']]
    anchors += [(Path(e['path']), e['branch_version']) for e in found['enterprise'] if e['branch_version']]
    anchors += [(Path(p['path']), next(iter(p['manifest_versions'])))
                for p in found['projects'] if len(p['manifest_versions']) == 1]
    for item in found['enterprise'] + found['projects']:
        item['location_version'] = _location_version(Path(item['path']), anchors, root)
    for kind in ('community', 'enterprise', 'venvs', 'projects'):
        for item in found[kind]:
            folder = _version_folder(Path(item['path']), root)
            item['version_folder'] = folder and str(folder)
    found['version_folders'] = version_folders(found)
    return found


# ─── CLI ─────────────────────────────────────────────────────────────────────

def _cli():
    p = argparse.ArgumentParser(description="Inspect the local Odoo workspace.")
    sub = p.add_subparsers(dest='mode', required=True)
    show = sub.add_parser('show', help='Resolved paths for a run')
    show.add_argument('--conf', help='Workspace conf (default: $ODOO_WORKSPACE_CONF)')
    show.add_argument('--target', help='Addons dir or worktree to detect the version from')
    show.add_argument('--odoo-version', help='Use this section instead of detecting')
    finder = sub.add_parser('find', help='Discover Odoo checkouts, venvs and project repos under a folder')
    finder.add_argument('root')
    args = p.parse_args()

    if args.mode == 'find':
        print(json.dumps(find(args.root), indent=2))
        return
    conf = Conf(args.conf)
    cfg = conf.pick(args.odoo_version, args.target)
    rows = {
        'conf': cfg.source,
        'version': f"{cfg.version} ({cfg.reason})",
        'community': cfg.community,
        'enterprise': cfg.enterprise or '-',
        'python': cfg.python,
        'projects': ', '.join(map(str, cfg.projects)) or '-',
        'addons path': cfg.addons_path(*([args.target] if args.target else [])),
        'configured versions': ', '.join(conf.sections),
    }
    for key, value in rows.items():
        print(f"{key:20} {value}")


if __name__ == '__main__':
    _cli()
