#!/usr/bin/env python3
"""
Run Odoo tests with automatic database management.

Usage:
    run_tests.py MODULE                                  Python and JS tests for a module
    run_tests.py MODULE_A,MODULE_B                       several modules in one run
    run_tests.py all                                     every installable module in the addons dir
    run_tests.py MODULES --python                        Python tests only
    run_tests.py MODULES --js                            JS unit tests only (Hoot, so 18.0+)
    run_tests.py MODULE TestClass.test_method            single Python test (implies --python)
    run_tests.py MODULES --fresh                         force a fresh database
    run_tests.py MODULES --custom-root=PATH              modules from another addons dir or worktree
    run_tests.py MODULES --odoo-version=18.0             when version detection fails
    run_tests.py --cleanup                               drop all test databases

Odoo, the venv and the project repos come from the workspace conf
(odoo_workspace.py). The version section is detected from --custom-root, which
can be left out when that version has a single project.

Each checkout gets one database, odoo-tests-<repo>-<branch>. The first run
creates it; later runs reuse it, installing any requested module it doesn't
have yet and updating the rest. So a module's tests run with whatever else
earlier runs installed there, the way Odoo.sh runs them with everything
installed. Use --fresh for a database with only the requested modules.

Both suites run in one odoo-bin process. Full output is kept at
$ODOO_DEV_LOG_DIR (default /tmp) as <db>-tests.log. When tests fail, the ERROR
blocks from that log (failed assertions, tracebacks) are printed and the script
exits with odoo-bin's status.

JS tests open a headless Chrome on /web/tests: the same WebSuite.test_unit_desktop
Odoo.sh runs, filtered to the given modules. Only modules whose manifest adds
at least one .test.js file to web.assets_unit_tests go in the filter, because Hoot drops a filter
that matches nothing and runs Odoo's entire JS suite. As a backstop, a run is
stopped as soon as Hoot starts a test outside the requested modules. Hoot
arrived in 18.0; earlier versions run the Python tests only.
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import odoo_workspace as ws  # noqa: E402

DB_PREFIX = 'odoo-tests'
LOG_DIR = ws.LOG_DIR
# 8169 avoids a developer-run Odoo on the default 8069; free_port climbs past parallel runs.
BASE_PORT = int(os.environ.get('ODOO_TEST_PORT', 8169))
SECONDS_PER_MODULE = 300
PROGRESS_RE = re.compile(r'Starting .*(test_|Test)|HOOT|failed|error.*of.*tests|succeeded')
HOOT_TEST_RE = re.compile(r'\[HOOT\] Running test "@([^/"]+)')
ESCAPED_FILTER = 3
TIMESTAMP_RE = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}')
MAX_DETAIL_LINES = 200


def print_failure_details(log):
    """Print the ERROR blocks from a run log.

    Each block starts at a timestamped ERROR line (Python tracebacks, "[HOOT] Test ... failed:"
    assertion lists) or a browser-console "[ERROR]" line (JS stack traces) and runs until the
    next timestamped line.
    """
    details, show = [], False
    for line in log.read_text(errors='replace').splitlines():
        if TIMESTAMP_RE.match(line):
            show = False
        if ' ERROR ' in line or '[ERROR]' in line:
            show = True
        if show:
            details.append(line)
    print(f"\n=== Failure details ({log}) ===")
    print('\n'.join(details[:MAX_DETAIL_LINES]))
    if MAX_DETAIL_LINES < len(details):
        print(f"... (truncated at {MAX_DETAIL_LINES} lines; full details in {log})")


def run_logged(cmd, log, timeout, hoot_modules=()):
    """Run cmd, keeping its full output in log and echoing only progress lines. Returns its status.

    Stops the run if Hoot starts a test from a module outside hoot_modules, which means it dropped the filter.
    """
    with open(log, 'w') as out:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        try:
            for line in proc.stdout:
                out.write(line)
                if PROGRESS_RE.search(line):
                    sys.stdout.write(line)
                    sys.stdout.flush()
                escaped = HOOT_TEST_RE.search(line)
                if escaped and escaped.group(1) not in hoot_modules:
                    proc.kill()
                    print(f"STOPPED: Hoot ignored the module filter and began running @{escaped.group(1)} "
                          "tests, which would run Odoo's entire JS suite.", flush=True)
                    return ESCAPED_FILTER
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            print(f"Timed out after {timeout}s", flush=True)
            return 124


def cleanup():
    print(f"Dropping all {DB_PREFIX}-* databases...")
    names = ws._psql(f"SELECT datname FROM pg_database WHERE datname LIKE '{DB_PREFIX}-%'")
    for name in filter(None, names.splitlines()):
        print(f"  Dropping: {name}")
        subprocess.run(['dropdb', '--if-exists', name], check=False)
    print("Done.")


def main():
    p = argparse.ArgumentParser(description="Run Odoo tests against a reusable database.")
    p.add_argument('modules', nargs='?', help='MODULE, MODULE_A,MODULE_B, or all')
    p.add_argument('filter', nargs='?', help='TestClass or TestClass.test_method (implies --python)')
    only = p.add_mutually_exclusive_group()
    only.add_argument('--python', action='store_true', help='Run only the Python tests')
    only.add_argument('--js', action='store_true', help='Run only the Hoot JS unit tests (18.0+)')
    p.add_argument('--fresh', action='store_true', help='Drop and recreate the database')
    p.add_argument('--custom-root', help="Addons dir or worktree to test (default: the version's project, "
                                         "when it has exactly one)")
    p.add_argument('--odoo-version', help='Conf section to use, when detecting it fails')
    p.add_argument('--conf', help='Workspace conf (default: $ODOO_WORKSPACE_CONF)')
    p.add_argument('--cleanup', action='store_true', help=f'Drop every {DB_PREFIX}-* database')
    args = p.parse_args()

    if args.cleanup:
        return cleanup()
    if not args.modules:
        p.error('MODULE is required')
    if args.filter and args.js:
        p.error("a TestClass.test_method filter selects Python tests, so it can't be used with --js")

    conf = ws.Conf(args.conf)
    root = Path(args.custom_root).expanduser().resolve() if args.custom_root else None
    cfg = conf.pick(args.odoo_version, root)
    print(f"Odoo {cfg.version} ({cfg.reason})")
    if not root:
        if len(cfg.projects) != 1:
            listed = ', '.join(map(str, cfg.projects)) or 'none'
            sys.exit(f"[{cfg.version}] has {len(cfg.projects)} projects ({listed}); pass --custom-root=PATH")
        root = cfg.projects[0]

    modules = ws.installable_modules(root) if args.modules == 'all' else args.modules.split(',')
    if not modules:
        sys.exit(f"No installable modules in {root}")
    modules_csv = ','.join(modules)
    print(f"Modules ({len(modules)}): {modules_csv}")

    run_python = not args.js
    js_modules = [] if args.python or args.filter else _js_modules(cfg, root, modules, required=args.js)

    repo, branch = ws.checkout_ref(root)
    tag = f"{repo or root}@{branch}"
    db, exists = ws.claim_db(DB_PREFIX, f"{(repo or root).name}-{branch}", tag)
    if args.fresh and exists:
        print(f"Dropping existing database: {db}")
        subprocess.run(['dropdb', db], check=True)
        exists = False
    if not exists:
        cfg.create_db(db, root, modules_csv, tag)

    port = ws.free_port(BASE_PORT)
    if port != BASE_PORT:
        print(f"Port {BASE_PORT} is busy; using port {port} instead.")

    tags = []
    if run_python:
        tags += [f'*/{m}' + (f':{args.filter}' if args.filter else '') for m in modules]
        print(f"Python tests: {', '.join(tags)}")
    if js_modules:
        hoot_filter = ','.join(f'@{m}' for m in js_modules)
        # WebSuite's [params] reach get_hoot_filters, which turns them into the URL's hash filter.
        tags.append(f'*/web:WebSuite.test_unit_desktop[{hoot_filter}]')
        print(f"JS tests: {hoot_filter}")
    # -i only touches modules the database doesn't have yet and -u only ones it does, so passing
    # both installs newly requested modules and updates the rest. WebSuite belongs to web, and
    # Odoo only runs tests of modules updated in the run, so the JS suite needs web in -u.
    updates = f'web,{modules_csv}' if js_modules else modules_csv
    cmd = cfg.odoo(db, root, f'--http-port={port}', '-i', modules_csv, '-u', updates,
                   f"--test-tags={','.join(tags)}", '--stop-after-init')
    log = LOG_DIR / f'{db}-tests.log'
    print(f"Database: {db}")
    print(f"Full log: {log}")
    status = run_logged(cmd, log, timeout=SECONDS_PER_MODULE * len(modules), hoot_modules=js_modules)
    if status and status != ESCAPED_FILTER:
        print_failure_details(log)
    sys.exit(status)


def _js_modules(cfg, root, modules, required):
    """The modules to filter Hoot to, printing why any are left out.

    required (--js) turns "no JS tests to run" into an error; otherwise the run goes on with Python only.
    """
    def skip(reason):
        if required:
            sys.exit(f"JS tests not run: {reason} Nothing was tested.")
        print(f"NOTE: JS tests skipped: {reason}", flush=True)
        return []

    if not ws.has_hoot(cfg):
        return skip(f"Odoo {cfg.version} has no Hoot (added in 18.0), and earlier versions' "
                    "QUnit suites can't be filtered per module.")
    js_modules = ws.hoot_modules(root, modules)
    if not js_modules:
        return skip("none of the modules adds a .test.js file to web.assets_unit_tests.")
    without = [m for m in modules if m not in js_modules]
    if without:
        print(f"NOTE: no JS tests in {', '.join(without)}", flush=True)
    return js_modules


if __name__ == '__main__':
    main()
