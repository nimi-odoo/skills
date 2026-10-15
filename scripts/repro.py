#!/usr/bin/env python3
"""
Reproduce arbitrary Odoo issues against a branch's exact code, in a worktree of the repro's own.

CLI usage:
    python repro.py <target> <bug_script.py> [--name=SLUG] [--modules=A,B,C] [--fresh] [--conf=PATH]
    python repro.py --ref=<ref> <bug_script.py> [--name=SLUG] ...
    python repro.py <target> --serve [--name=SLUG] [--port=8269] ...
    python repro.py --stop --name=SLUG

--serve returns once the server accepts requests, printing a `>>> READY: <url>` line. odoo-bin
keeps running on its own, logging to <tmp>/odoo-repro-<name>.log, until --stop ends it; --stop
returns once it's gone.

Every repro runs in its own detached worktree in the job tmp dir; the code it starts
from is never edited. <target> says where to start: a branch name starts from
origin/<branch> (fetched first), a path to an existing worktree starts from that
worktree's current commit, and --ref starts from any ref or commit.

Each bug gets its own database, odoo-repro-<name>, and worktree, <version>-repro-<name>, where
--name is a short slug for what's being reproduced (default: <repo>-<branch>). The
database is tagged with the repo and branch it was built from, so a name already
used for other code moves on to <name>-2 instead of mixing the two. Re-runs reuse
both, so a fix applied in the worktree survives.

Paths to Odoo, the venv and the project repos come from the workspace conf (odoo_workspace.py).

Library usage (from inside a bug script run via odoo-bin shell):
    from repro import as_user, isolate_field_read, spy_method
"""

import argparse
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from contextlib import suppress
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import odoo_workspace as ws  # noqa: E402

PREFIX = 'odoo-repro'
SERVE_TIMEOUT = 120  # seconds for odoo-bin to accept requests; installs are done before it starts
STOP_TIMEOUT = 15  # seconds for odoo-bin to shut down on SIGTERM before it gets SIGKILL


# ─── Library helpers ─────────────────────────────────────────────────────────

def as_user(env, login, group_xmlids=(), password=None):
    """Get or create a res.users with login + groups. Its password is the login unless given, for browser login."""
    User = env['res.users']
    user = User.search([('login', '=', login)], limit=1)
    if not user:
        # Renamed from groups_id in 19.0
        groups_field = 'group_ids' if 'group_ids' in User._fields else 'groups_id'
        user = User.create({
            'login': login,
            'password': password or login,
            'name': login.replace('_', ' ').title(),
            groups_field: [(6, 0, [env.ref(xid).id for xid in group_xmlids])],
        })
    return user


def isolate_field_read(record, user, fields=None):
    """Read each field individually under user. Returns [(fname, exc_or_None), ...]."""
    fields = fields or list(record._fields)
    results = []
    for fname in fields:
        try:
            record.with_user(user).read([fname], load=False)
            results.append((fname, None))
        except Exception as e:
            results.append((fname, e))
    return results


def spy_method(env, model, method_name, stack_depth=5):
    """Monkey-patch model.method_name to print args + stack. Returns restore fn."""
    cls = type(env[model])
    original = getattr(cls, method_name)
    def _spy(self, *args, **kwargs):
        print(f">> {model}.{method_name} args={args!r} kwargs={kwargs!r}")
        if stack_depth:
            print("".join(traceback.format_stack()[-stack_depth - 1 : -1]))
        return original(self, *args, **kwargs)
    setattr(cls, method_name, _spy)
    return lambda: setattr(cls, method_name, original)


# ─── CLI ─────────────────────────────────────────────────────────────────────

def _tmp_root():
    """Where throwaway worktrees live: the job tmp dir, so they get cleaned with the job."""
    job_dir = os.environ.get('CLAUDE_JOB_DIR')
    root = Path(job_dir) / 'tmp' if job_dir else Path(tempfile.gettempdir())
    root.mkdir(parents=True, exist_ok=True)
    return root


def _is_path(target):
    return os.sep in str(target) or str(target).startswith(('~', '.'))


def _require_repo(repo):
    if not repo:
        sys.exit("No project repo: add it to `projects` in the conf's version section, or pass --repo=PATH")
    if not (repo / '.git').exists():
        sys.exit(f"Not a git checkout: {repo}")
    return repo


def _git(repo, *cmd):
    if subprocess.run(['git', '-C', str(repo), *cmd]).returncode:
        sys.exit(f"git {' '.join(cmd)} failed in {repo}")


def _git_ok(repo, *cmd):
    return not subprocess.run(['git', '-C', str(repo), *cmd], capture_output=True).returncode


def _on_origin(repo, branch):
    """Whether origin has branch: from the remote-tracking ref when there is one, else by asking origin."""
    return (_git_ok(repo, 'rev-parse', '--verify', '-q', f'refs/remotes/origin/{branch}')
            or _git_ok(repo, 'ls-remote', '--exit-code', '--heads', 'origin', branch))


def _branch_repo(branch, repos):
    """The one project repo whose origin has branch."""
    if not repos:
        _require_repo(None)
    matches = [repo for repo in repos if _on_origin(_require_repo(repo), branch)]
    if not matches:
        sys.exit(f"No project repo has origin/{branch} ({', '.join(map(str, repos))}). "
                 "Pass --repo=PATH, or --ref=<commit> for a commit that isn't on a branch.")
    if 1 < len(matches):
        sys.exit(f"origin/{branch} exists in several repos ({', '.join(map(str, matches))}); pass --repo=PATH")
    return matches[0]


def _ref_repo(conf, repo, version, ref):
    """The repo to create a --ref worktree from, before there's a worktree to detect a version from."""
    if repo:
        return repo
    projects = conf.projects()
    if len(projects) == 1:
        return projects[0]
    cfg = conf.pick(version, branch=ref)
    if len(cfg.projects) != 1:
        listed = ', '.join(map(str, cfg.projects)) or 'none'
        sys.exit(f"[{cfg.version}] has {len(cfg.projects)} projects ({listed}); pass --repo=PATH")
    return cfg.projects[0]


def _resolve_base(conf, args, repo):
    """(repo, ref, source) the repro starts from; source names it for the database tag and label.

    A branch name starts from origin/<branch>, what's deployed. A worktree path starts from that
    worktree's current commit. --ref starts from any ref or commit. The source itself is never edited.
    """
    if args.ref:
        return _ref_repo(conf, repo, args.odoo_version, args.ref), args.ref, args.ref
    if _is_path(args.target):
        path = Path(args.target).expanduser().resolve()
        if not path.is_dir():
            sys.exit(f"Worktree path not found: {path}")
        head = ws._git_out(path, 'rev-parse', 'HEAD') or sys.exit(f"Not a git checkout: {path}")
        return _require_repo(ws.main_checkout(path)), head, ws.current_branch(path) or path.name
    repo = repo or _branch_repo(args.target, conf.projects())
    return repo, f'origin/{args.target}', args.target


def _note_local_ahead(repo, branch):
    """Say so when the local branch has commits origin doesn't, since the repro leaves them out."""
    ahead = ws._git_out(repo, 'rev-list', '--count', f'origin/{branch}..refs/heads/{branch}')
    if ahead and ahead != '0':
        print(f"NOTE: local {branch} has {ahead} commit(s) not on origin/{branch}; "
              f"the repro uses origin/{branch}.", flush=True)


def _repro_worktree(conf, args, repo, ref, source, db):
    """This repro's own detached worktree, <version>-repro-<name>, created from ref or reused as left.

    Returns (worktree, cfg). The version is read from the checked-out code, so a new worktree is
    created under a provisional name and renamed once its version is known.
    """
    stem = db.removeprefix('odoo-')
    existing = [path for path in _tmp_root().glob(f'*-{stem}')
                if re.fullmatch(r'(saas~)?\d+\.\d+', path.name.removesuffix(f'-{stem}'))]
    if existing:
        worktree = existing[0]
        # A reused worktree keeps whatever fix is being tested in it, so leave HEAD alone.
        print(f"Reusing worktree {worktree}", flush=True)
    else:
        worktree = _tmp_root() / f'new-{stem}'
        # Left by a run that died before renaming it; never used, so start over.
        if worktree.exists():
            _git(repo, 'worktree', 'remove', '--force', str(worktree))
        remote, _, branch = ref.partition('/')
        if branch and remote in ('origin', 'upstream'):
            _git(repo, 'fetch', '-q', remote, branch)
        _git(repo, 'worktree', 'add', '-q', '--detach', str(worktree), ref)
    cfg = conf.pick(args.odoo_version, worktree, branch=source)
    if not existing:
        # Rename before filling submodules: git refuses to move a worktree that has them.
        final = _tmp_root() / f'{cfg.version}-{stem}'
        _git(repo, 'worktree', 'move', str(worktree), str(final))
        worktree = final
        print(f"Created worktree {worktree} from {ref}", flush=True)
    # git worktree add leaves submodules empty, and the worktree is this repro's own, so fill them.
    if not all(initialized for _path, initialized in ws.submodules(worktree)):
        ws.init_submodules(worktree)
    return worktree, cfg


def _source_worktree(args, repo):
    """The user's worktree the repro started from, where a fix could be applied: the path given,
    or the worktree that has the branch checked out. None when there isn't one."""
    if args.ref:
        return None
    if _is_path(args.target):
        return Path(args.target).expanduser().resolve()
    out = ws._git_out(repo, 'worktree', 'list', '--porcelain') or ''
    path = None
    for line in out.splitlines():
        if line.startswith('worktree '):
            path = Path(line.removeprefix('worktree '))
        elif line == f'branch refs/heads/{args.target}':
            return path
    return None


def _run_bug_script(cfg, db, worktree, bug_script, env_vars=None):
    scripts_dir = Path(__file__).resolve().parent
    env = {**os.environ, **(env_vars or {}),
           'PYTHONPATH': f"{scripts_dir}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"}
    with open(bug_script) as f:
        return subprocess.run(cfg.odoo(db, worktree, '--no-http', '--log-level=warn', command='shell'),
                              env=env, stdin=f, check=False)


def _host(db):
    """A hostname of its own for this repro, since browsers share cookies across localhost ports."""
    return re.sub(r'[^a-z0-9-]+', '-', db.lower()).strip('-')[:63] + '.localhost'


# The line odoo-bin logs once it accepts requests.
READY_RE = re.compile(r'HTTP service \(.*\) running|HTTP service running', re.IGNORECASE)


def _log(db):
    """The log db's odoo-bin writes to while it serves."""
    return _tmp_root() / f'{db}.log'


def _servers(prefix):
    """(pid, db, port) of every running odoo-bin serving a database whose name starts with prefix.

    Matched on the command's arguments rather than its text, so a shell or grep whose command
    line mentions a server isn't taken for one.
    """
    out = subprocess.run(['ps', '-axww', '-o', 'pid=,stat=,args='], capture_output=True, text=True).stdout
    servers = []
    for line in out.splitlines():
        pid, stat, *argv = line.split()
        db = dict(zip(argv, argv[1:])).get('-d', '')
        ports = [arg.removeprefix('--http-port=') for arg in argv if arg.startswith('--http-port=')]
        is_odoo_bin = any(arg.endswith('/odoo-bin') for arg in argv[:2])
        if is_odoo_bin and not stat.startswith('Z') and db.startswith(prefix) and ports:
            servers.append((int(pid), db, int(ports[0])))
    return servers


def _running(pid):
    """Whether pid is alive; one that exited but isn't reaped yet (state Z) isn't."""
    stat = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(stat) and not stat.startswith('Z')


def _gone(pid, timeout):
    """Whether pid is gone within timeout seconds."""
    deadline = time.monotonic() + timeout
    while _running(pid):
        if deadline < time.monotonic():
            return False
        time.sleep(0.2)
    return True


def _terminate(pid):
    """End pid, returning once it's gone."""
    with suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
        if not _gone(pid, STOP_TIMEOUT):
            os.kill(pid, signal.SIGKILL)
            _gone(pid, STOP_TIMEOUT)


def _stop(db):
    """End the servers of db, returning once they're gone, so a start right after gets the port and log to itself."""
    servers = [s for s in _servers(db) if s[1] == db]
    for pid, _db, port in servers:
        _terminate(pid)
        print(f"Stopped {db} on port {port} (pid {pid})", flush=True)
    if not servers:
        print(f"No server running for {db}", flush=True)


def _serve(cfg, db, worktree, port, *extra_flags):
    """Start odoo-bin on its own and return once it accepts requests; exit with the end of its log when it doesn't."""
    log = _log(db)
    with log.open('wb') as out:
        # A session of its own keeps odoo-bin running after this script exits, out of reach of its terminal's signals.
        # The db filter keeps a session cookie from another database from being accepted here.
        proc = subprocess.Popen(
            cfg.odoo(db, worktree, f'--http-port={port}', '--workers=0', f'--db-filter=^{re.escape(db)}$',
                     *extra_flags),
            stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
        )
    print(f"Server log: {log}", flush=True)
    deadline = time.monotonic() + SERVE_TIMEOUT
    while proc.poll() is None and time.monotonic() < deadline:
        if READY_RE.search(log.read_text(errors='replace')):
            print(f">>> READY: http://{_host(db)}:{port}/web (db={db})", flush=True)
            return
        time.sleep(0.5)
    timed_out = proc.poll() is None
    if timed_out:
        _terminate(proc.pid)
    print(''.join(log.read_text(errors='replace').splitlines(keepends=True)[-40:]), end='', flush=True)
    reason = f"odoo-bin didn't accept requests within {SERVE_TIMEOUT}s" if timed_out else "odoo-bin stopped before serving"
    sys.exit(f"{reason}; see the end of its log above")


def main():
    p = argparse.ArgumentParser(description="Reproduce an Odoo issue on a worktree branch.")
    p.add_argument('target', nargs='?',
                   help='Branch to start from (origin/<branch> in a project repo), or a path to a '
                        'worktree to start from its current commit. Omit when passing --ref.')
    p.add_argument('bug_script', type=Path, nargs='?',
                   help='Path to bug-reproduction .py (shell mode). Omit with --serve.')
    p.add_argument('--name', help='Short slug for what is being reproduced; the DB is odoo-repro-<name>')
    p.add_argument('--conf', help='Workspace conf (default: $ODOO_WORKSPACE_CONF)')
    p.add_argument('--repo', type=Path,
                   help='Project git repo (default: whichever `projects` repo has the branch)')
    p.add_argument('--odoo-version',
                   help='Conf section to use, when detecting it from the worktree fails')
    p.add_argument('--ref', help='Any git ref or commit to start from, instead of <target>')
    p.add_argument('--modules', default='',
                   help='Comma-separated modules the bug needs; any the DB lacks are installed first')
    p.add_argument('--fresh', action='store_true', help='Drop and recreate the DB')
    p.add_argument('--serve', action='store_true',
                   help='Start an HTTP server on --port instead of running a bug script')
    p.add_argument('--port', type=int, default=8269,
                   help='Lowest HTTP port to try for --serve; climbs if taken '
                        '(default 8269; 8069=dev, 8169=tests)')
    p.add_argument('--stop', action='store_true', help="Stop --name's server, returning once it's gone")
    args = p.parse_args()

    if args.stop and not args.name:
        sys.exit("--stop needs --name")
    if args.stop:
        _stop(ws.db_name(PREFIX, args.name))
        return

    # --ref supplies the worktree, so a lone positional there is the bug script.
    if args.ref and args.target and not args.bug_script:
        args.target, args.bug_script = None, Path(args.target)
    if bool(args.target) == bool(args.ref):
        sys.exit("Provide exactly one of <target> or --ref")
    if args.serve and args.bug_script:
        sys.exit("--serve and bug_script are mutually exclusive")
    if not args.serve and not args.bug_script:
        sys.exit("Provide a bug_script, or pass --serve")
    if args.bug_script and not args.bug_script.exists():
        sys.exit(f"Bug script not found: {args.bug_script}")

    conf = ws.Conf(args.conf)
    repo = args.repo.expanduser().resolve() if args.repo else None
    repo, ref, source = _resolve_base(conf, args, repo)
    if args.target and not args.ref and not _is_path(args.target):
        _note_local_ahead(repo, args.target)

    tag = f"{repo}@{source}"
    db, exists = ws.claim_db(PREFIX, args.name or f"{repo.name}-{source}", tag)
    worktree, cfg = _repro_worktree(conf, args, repo, ref, source, db)
    print(f"Worktree: {worktree} (from {ref})", flush=True)
    source_worktree = _source_worktree(args, repo)
    if source_worktree:
        print(f"Source worktree: {source_worktree} (never edited by the repro)", flush=True)
    print(f"Odoo {cfg.version} ({cfg.reason})", flush=True)
    print(f"Database: {db}", flush=True)

    running = [s for s in _servers(db) if s[1] == db]
    if args.serve and running:
        sys.exit(f"{db} is already served on port {running[0][2]} (pid {running[0][0]}); "
                 f"`repro.py --stop --name={db.removeprefix(PREFIX + '-')}` first")
    if args.fresh and exists:
        print(f"Dropping {db} (--fresh)", flush=True)
        subprocess.run(['dropdb', db], check=True)
        exists = False
    if not exists:
        cfg.create_db(db, worktree, args.modules, tag)
    else:
        print(f"Reusing existing {db}", flush=True)
        cfg.install_missing(db, worktree, args.modules)

    if args.serve:
        port = ws.free_port(args.port)
        if port != args.port:
            print(f">>> Port {args.port} taken, serving on {port}", flush=True)
        _serve(cfg, db, worktree, port)
    else:
        _run_bug_script(cfg, db, worktree, args.bug_script)


if __name__ == '__main__':
    ws.report_failure(main)
