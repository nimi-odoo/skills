#!/usr/bin/env python3
"""
Spin up a local Odoo server with a database of its own, to show a workflow running.

CLI usage:
    spin.py start --name=SLUG [<target>] [--modules=A,B] [--update=A,B] [--fresh] [--port=8369] [--conf=PATH]
    spin.py seed --name=SLUG <generator.py> [key=value ...]
    spin.py generators
    spin.py list
    spin.py stop --name=SLUG
    spin.py cleanup --name=SLUG

<target> is the custom code to serve: a path to a worktree or addons dir, or a branch
name, which means the worktree that has it checked out in one of the conf's projects.
Leave it out to serve Community (and Enterprise) only. Unlike repro.py, the code is
served where it is, with --dev=reload,xml, so edits made there show up on the server.

start returns once the server accepts requests, printing a `>>> READY: <url>` line, or
a `>>> FAILED: <reason>` line when it doesn't get there. odoo-bin keeps running on its
own, logging to <tmp>/odoo-spin-<name>.log, until `stop` ends it; stop returns once
it's gone, so a start right after it gets the port and the log to itself.

Each spin gets its own database, odoo-spin-<name>, tagged with the version and the
code it serves. A name already used for other code moves on to <name>-2; the run
prints the name it used, which the other commands take.

A generator is a Python file run in odoo-bin shell against the spin's database, with
`env` as a global. Its docstring says what it creates and which arguments it takes;
it reads them with seed_args(), and it's safe to run twice. Folders of them go in the
conf's `generators` key, which `spin.py generators` lists.

Library usage (from inside a generator):
    from spin import seed_args
    args = seed_args(count=100, company='My Company')   # defaults, cast to their types
"""

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import odoo_workspace as ws  # noqa: E402
from repro import _run_bug_script, _serve, _servers, _stop, _tmp_root  # noqa: E402

PREFIX = 'odoo-spin'
DEFAULT_PORT = 8369  # 8069 = dev, 8169 = run_tests.py, 8269 = repro.py
TAG_RE = re.compile(r'^spin \[(?P<version>[^\]]+)\] (?P<addons>.*)$')
NO_ADDONS = '(no custom addons)'
# odoo-bin shell exits 0 when its script raises, so a generator runs through this to fail loudly.
SEED_WRAPPER = '''\
import os, sys, traceback
try:
    exec(compile(open({path!r}).read(), {path!r}, 'exec'))
except Exception:
    traceback.print_exc()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1)
'''


# ─── Library helper ──────────────────────────────────────────────────────────

def seed_args(**defaults):
    """The key=value arguments `spin.py seed` was given, over defaults, each cast to its default's type."""
    given = json.loads(os.environ.get('SPIN_SEED_ARGS') or '{}')
    args = dict(defaults)
    for key, value in given.items():
        default = defaults.get(key)
        if isinstance(default, bool):
            args[key] = value.lower() in ('1', 'true', 'yes', 'y')
        elif isinstance(default, (int, float)):
            args[key] = type(default)(value)
        else:
            args[key] = value
    return args


# ─── CLI ─────────────────────────────────────────────────────────────────────

def _is_path(target):
    return os.sep in str(target) or str(target).startswith(('~', '.'))


def _branch_worktree(branch, repos):
    """The worktree that has branch checked out, in one of repos."""
    found = []
    for repo in repos:
        path = None
        for line in (ws._git_out(repo, 'worktree', 'list', '--porcelain') or '').splitlines():
            if line.startswith('worktree '):
                path = Path(line.removeprefix('worktree '))
            elif line == f'branch refs/heads/{branch}':
                found.append(path)
    if not found:
        sys.exit(f"No worktree has {branch} checked out in {', '.join(map(str, repos)) or 'the conf projects'}. "
                 "Pass a worktree path, or check the branch out first.")
    if 1 < len(found):
        sys.exit(f"{branch} is checked out in several worktrees ({', '.join(map(str, found))}); pass one's path")
    return found[0]


def _addons(conf, args):
    """The custom addons dir to serve live, or None for Community/Enterprise only."""
    if not args.target:
        return None
    if _is_path(args.target):
        path = Path(args.target).expanduser().resolve()
        if not path.is_dir():
            sys.exit(f"Not a folder: {path}")
        return path
    repos = [args.repo.expanduser().resolve()] if args.repo else conf.projects()
    return _branch_worktree(args.target, repos)


def _db(name):
    db = ws.db_name(PREFIX, name)
    if not ws.db_exists(db):
        sys.exit(f"No database {db}. Start it first with `spin.py start --name={name}`.")
    return db


def _spun(db):
    """(version, addons) a spin's database was built for, from its tag."""
    match = TAG_RE.match(ws.db_tag(db) or '')
    if not match:
        sys.exit(f"{db} wasn't made by spin.py (tag: {ws.db_tag(db) or 'none'})")
    addons = match['addons']
    return match['version'], None if addons == NO_ADDONS else Path(addons)


def _filestores(db):
    """Where Odoo's default data dir keeps db's attachments, on macOS and Linux."""
    home = Path.home()
    return [base / 'filestore' / db for base in (
        home / 'Library' / 'Application Support' / 'Odoo',
        Path(os.environ.get('XDG_DATA_HOME', home / '.local' / 'share')) / 'Odoo',
    )]


def _drop(db):
    """Drop db and its filestore."""
    subprocess.run(['dropdb', '--if-exists', '--force', db], check=True)
    print(f"Dropped {db}", flush=True)
    for filestore in _filestores(db):
        if filestore.is_dir():
            shutil.rmtree(filestore)
            print(f"Removed its filestore {filestore}", flush=True)


def cmd_start(conf, args):
    addons = _addons(conf, args)
    cfg = conf.pick(args.odoo_version, addons)
    tag = f"spin [{cfg.version}] {addons or NO_ADDONS}"
    db, exists = ws.claim_db(PREFIX, args.name, tag)
    print(f"Odoo {cfg.version} ({cfg.reason})", flush=True)
    print(f"Name: {db.removeprefix(PREFIX + '-')}", flush=True)
    print(f"Database: {db}", flush=True)
    print(f"Addons: {addons or NO_ADDONS}" + (" (served live)" if addons else ''), flush=True)

    running = [s for s in _servers(db) if s[1] == db]
    if running:
        sys.exit(f"{db} is already served on port {running[0][2]} (pid {running[0][0]}); "
                 f"`spin.py stop --name={args.name}` first")
    if args.fresh and exists:
        print(f"Dropping {db} (--fresh)", flush=True)
        _drop(db)
        exists = False
    if not exists:
        cfg.create_db(db, addons, args.modules, tag)
    else:
        print(f"Reusing existing {db}", flush=True)
        cfg.install_missing(db, addons, args.modules)
    if args.update:
        print(f"Updating in {db}: {args.update}", flush=True)
        cfg.install(db, addons, args.update, flag='-u')

    port = ws.free_port(args.port)
    if port != args.port:
        print(f">>> Port {args.port} taken, serving on {port}", flush=True)
    print("Login: admin / admin", flush=True)
    _serve(cfg, db, addons, port, '--dev=reload,xml')


def cmd_seed(conf, args):
    db = _db(args.name)
    version, addons = _spun(db)
    if not args.generator.is_file():
        sys.exit(f"Generator not found: {args.generator}")
    pairs = {}
    for pair in args.params:
        key, sep, value = pair.partition('=')
        if not sep:
            sys.exit(f"Generator arguments are key=value, got {pair!r}")
        pairs[key] = value
    cfg = conf.pick(version)
    print(f"Seeding {db} with {args.generator}" + (f" ({', '.join(args.params)})" if pairs else ''), flush=True)
    wrapper = _tmp_root() / f'seed-{db}.py'
    wrapper.write_text(SEED_WRAPPER.format(path=str(args.generator.resolve())))
    result = _run_bug_script(cfg, db, addons, wrapper, {'SPIN_SEED_ARGS': json.dumps(pairs)})
    if result.returncode:
        sys.exit(f"Generator {args.generator} failed (exit {result.returncode}); see the traceback above")


def cmd_generators(conf, args):
    folders = conf.generators()
    if not folders:
        print("No generators: add `generators = <folder>` to a version section of the conf.")
        return
    for version, folder in folders:
        print(f"[{version}] {folder}")
        scripts = sorted(folder.glob('*.py')) if folder.is_dir() else []
        if not folder.is_dir():
            print("  (folder not found)")
        elif not scripts:
            print("  (no generators yet)")
        for script in scripts:
            try:
                doc = ast.get_docstring(ast.parse(script.read_text())) or ''
            except SyntaxError:
                doc = '(does not parse)'
            print(f"  {script.name}: {doc.splitlines()[0] if doc else '(no docstring)'}")


def cmd_list(conf, args):
    servers = _servers(PREFIX)
    print("Running:")
    for pid, db, port in servers:
        print(f"  {db}: http://{db}.localhost:{port}/web (pid {pid})")
    if not servers:
        print("  (none)")
    dbs = ws._psql(f"SELECT datname FROM pg_database WHERE datname LIKE '{PREFIX}-%' ORDER BY 1").splitlines()
    print("Databases:")
    for db in dbs:
        match = TAG_RE.match(ws.db_tag(db) or '')
        print(f"  {db}" + (f" [{match['version']}] {match['addons']}" if match else ''))
    if not dbs:
        print("  (none)")


def cmd_stop(conf, args):
    _stop(ws.db_name(PREFIX, args.name))


def cmd_cleanup(conf, args):
    cmd_stop(conf, args)
    db = _db(args.name)
    _spun(db)
    _drop(db)


def main():
    p = argparse.ArgumentParser(description="Spin up a local Odoo server with a database of its own.")
    p.add_argument('--conf', help='Workspace conf (default: $ODOO_WORKSPACE_CONF)')
    sub = p.add_subparsers(dest='command', required=True)

    start = sub.add_parser('start', help='Create or reuse the database and serve it; returns once it serves')
    start.add_argument('target', nargs='?',
                       help='Worktree/addons path or checked-out branch to serve live; omit for none')
    start.add_argument('--name', required=True, help='Short slug; the DB is odoo-spin-<name>')
    start.add_argument('--repo', type=Path, help='Project repo to find the branch in (default: conf projects)')
    start.add_argument('--odoo-version', help='Conf section to use, when detecting it fails')
    start.add_argument('--modules', default='', help='Comma-separated modules; any the DB lacks are installed')
    start.add_argument('--update', default='', help='Comma-separated modules to update (-u) before serving')
    start.add_argument('--fresh', action='store_true', help='Drop and recreate the DB')
    start.add_argument('--port', type=int, default=DEFAULT_PORT,
                       help=f'Lowest HTTP port to try; climbs if taken (default {DEFAULT_PORT})')

    seed = sub.add_parser('seed', help='Run a generator against a spin database')
    seed.add_argument('--name', required=True)
    seed.add_argument('generator', type=Path)
    seed.add_argument('params', nargs='*', help='key=value arguments for the generator')

    sub.add_parser('generators', help="List the generators in the conf's `generators` folders")
    sub.add_parser('list', help='Running spin servers and spin databases')
    for name, help_text in (('stop', 'Stop a spin server'),
                            ('cleanup', 'Stop a spin server, drop its database and filestore')):
        sub.add_parser(name, help=help_text).add_argument('--name', required=True)

    args = p.parse_args()
    conf = None if args.command in ('list', 'stop') else ws.Conf(args.conf)
    globals()[f'cmd_{args.command}'](conf, args)


if __name__ == '__main__':
    ws.report_failure(main)
