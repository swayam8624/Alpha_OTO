"""Loopback-only Alpha_OTO operator dashboard. NO broker orders or payments.

The website orchestrates existing offline research and paper-only workflows. It
cannot unlock live trading or charge a payment method. Broker token is memory only.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hmac
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
import webbrowser

ROOT = Path(__file__).resolve().parents[2]
STATIC = Path(__file__).resolve().parent / 'dashboard_static'
MAX_BODY = 8192
MAX_LOG = 16000
LIVE_REJECTION = 'Real-money orders, deposits, withdrawals and payments are not implemented or authorized.'


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _tail(path: Path, n=10):
    if not path.exists():
        return None
    try:
        with path.open('rb') as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 4096))
            lines = f.read().decode('utf-8', errors='replace').splitlines()
        return (lines[-1] if lines else None)
    except OSError:
        return None


def _json_file(path: Path, size_limit=4_000_000):
    try:
        if path.is_file() and path.stat().st_size <= size_limit:
            return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        pass
    return None


class DashboardService:
    """Durable local operator config and allowlisted single-flight jobs."""

    ACTIONS = {
        'tests': {'label': 'Run offline test suite', 'timeout': 300},
        'paper_start': {'label': 'Start forward-only paper monitoring', 'timeout': 300},
        'omega': {'label': 'Quantitative strategy research', 'timeout': 1800},
        'forward': {'label': 'Advance forward-only paper ledger', 'timeout': 240},
        'data': {'label': 'Refresh BTC/ETH completed candles', 'timeout': 180},
        'risk_smoke': {'label': 'Risk + simulated broker integration', 'timeout': 90},
        'l2_smoke': {'label': 'Level 2 + isolated risk integration', 'timeout': 90},
        'l2_capture': {'label': 'Read-only live Level 2 capture (30s)', 'timeout': 60},
    }

    def __init__(self, root=ROOT, *, start_scheduler=True):
        self.root = Path(root).resolve()
        from .dhan_readonly import DhanSession
        self.dhan = DhanSession()  # Secrets remain in volatile memory; never in SQLite.
        self.state_dir = self.root / 'private_data' / 'dashboard'
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.state_dir, 0o700)
        except OSError:
            pass
        self.db_path = self.state_dir / 'operator.sqlite3'
        self.jobs_dir = self.root / 'artifacts' / 'dashboard_jobs'
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    exit_code INTEGER,
                    log_path TEXT NOT NULL
                );
            ''')
            db.execute("UPDATE jobs SET status='INTERRUPTED', ended_at=? WHERE status='RUNNING'", (_utc(),))
            db.commit()
        self._thread = None
        if start_scheduler:
            self._thread = threading.Thread(target=self._scheduler_loop, name='alpha-paper-scheduler', daemon=True)
            self._thread.start()

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=8)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=8000')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _get(self, key, default=None):
        with self._db() as db:
            row = db.execute('SELECT value FROM config WHERE key=?', (key,)).fetchone()
        return json.loads(row['value']) if row else default

    def _put(self, key, value):
        with self._db() as db:
            db.execute('INSERT INTO config(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       (key, json.dumps(value, separators=(',', ':'))))
            db.commit()

    def setup(self):
        state = self._get('setup', {})
        return {
            'market': state.get('market', 'undecided'),
            'broker': state.get('broker', 'undecided'),
            'paper_budget': state.get('paper_budget', 10000),
            'paper_schedule_enabled': bool(self._get('schedule_enabled', False)),
            'mode': 'PAPER_ONLY',
            'broker_connected': self.dhan.status()['connected'],
            'broker_readonly': self.dhan.status(),
            'payments_connected': False,
            'real_orders_enabled': False,
            'steps': [
                {'id': 'machine', 'title': 'Local runtime', 'state': 'COMPLETE'},
                {'id': 'venue', 'title': 'Select market and broker', 'state': 'COMPLETE' if state.get('market') not in (None, 'undecided') and state.get('broker') not in (None, 'undecided') else 'PENDING'},
                {'id': 'paper', 'title': 'Paper trading and data validation', 'state': 'AVAILABLE'},
                {'id': 'identity', 'title': 'Dhan account verification (read-only)', 'state': 'COMPLETE' if self.dhan.status()['connected'] else 'EXTERNAL_REQUIRED'},
                {'id': 'funding', 'title': 'Funding / fee payments', 'state': 'EXTERNAL_REQUIRED'},
                {'id': 'approval', 'title': 'Live risk and legal approvals', 'state': 'NOT_AVAILABLE'},
            ],
        }

    def save_setup(self, payload):
        if not isinstance(payload, dict) or set(payload) != {'market', 'broker', 'paper_budget'}:
            raise ValueError('Expected market, broker and paper_budget only; do not submit credentials')
        market, broker, budget = payload['market'], payload['broker'], payload['paper_budget']
        if market not in ('undecided', 'crypto_spot', 'india_cash_equities', 'global_cash_equities'):
            raise ValueError('Unsupported market selection')
        if broker not in ('undecided', 'dhan', 'ibkr', 'other'):
            raise ValueError('Unsupported broker selection')
        if type(budget) not in (int, float) or not 100 <= budget <= 10_000_000:
            raise ValueError('Paper budget must be numeric between 100 and 10,000,000')
        state = {'market': market, 'broker': broker, 'paper_budget': round(float(budget), 2)}
        with self._lock:
            self._put('setup', state)
        return self.setup()

    def jobs(self, limit=20):
        with self._db() as db:
            rows = db.execute('SELECT * FROM jobs ORDER BY started_at DESC LIMIT ?', (min(100, max(1, limit)),)).fetchall()
        return [{k: row[k] for k in ('id','action','status','started_at','ended_at','exit_code')} for row in rows]

    def job(self, job_id):
        with self._db() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise ValueError('Unknown job')
        log = Path(row['log_path'])
        if log.parent != self.jobs_dir or log.is_symlink() or not log.name.endswith('.log'):
            raise ValueError('Invalid stored log path')
        try:
            with log.open('rb') as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - MAX_LOG))
                txt = f.read().decode('utf-8', errors='replace')
        except OSError:
            txt = ''
        return {**{k: row[k] for k in ('id','action','status','started_at','ended_at','exit_code')}, 'output': txt[-MAX_LOG:]}

    def _argv(self, action, job_id):
        python = sys.executable
        paths = ['private_data/BTC-USD_1h.csv', 'private_data/ETH-USD_1h.csv']
        if action == 'tests':
            return [python, '-m', 'unittest', 'discover', '-s', 'tests', '-q']
        if action == 'paper_start':
            return ['bash', 'scripts/start_paper_once.sh']
        if action == 'omega':
            return ['bash', 'scripts/run_omega_research.sh']
        if action == 'forward':
            return ['bash', 'scripts/forward_once.sh']
        if action == 'data':
            return None  # Two sequential allowlisted public-data read-only commands
        if action == 'risk_smoke':
            return [python, 'scripts/risk_isolated_smoke.py', '--outdir', f'artifacts/dashboard_jobs/{job_id}_risk']
        if action == 'l2_smoke':
            return [python, 'scripts/l2_isolated_smoke.py', '--outdir', f'artifacts/dashboard_jobs/{job_id}_l2']
        if action == 'l2_capture':
            return [python, '-m', 'alpha_oto.production.marketd', 'capture', '--source', 'advanced',
                    '--product', 'BTC-USD', '--seconds', '30', '--limit', '6000',
                    '--feed', f'private_data/market/dashboard_{job_id}.sqlite3',
                    '--raw-journal', f'private_data/market/dashboard_{job}.jsonl']
        raise ValueError('Action not available')

    def launch(self, action):
        if not isinstance(action, str) or action not in self.ACTIONS:
            raise ValueError(LIVE_REJECTION)
        with self._lock:
            with self._db() as db:
                active = db.execute("SELECT id FROM jobs WHERE status='RUNNING' LIMIT 1").fetchone()
                if active:
                    raise RuntimeError('Another operation is running. Wait for it to finish.')
                job_id = secrets.token_hex(8)
                log = self.jobs_dir / f'{job_id}.log'
                log.touch(mode=0o600, exist_ok=False)
                db.execute('INSERT INTO jobs(id,action,status,started_at,log_path) VALUES(?,?,?,?,?)',
                           (job_id, action, 'RUNNING', _utc(), str(log)))
                db.commit()
            worker = threading.Thread(target=self._run_job, args=(job_id, action, log), daemon=True)
            worker.start()
        return {'job_id': job_id, 'status': 'RUNNING', 'action': action}

    def _run_job(self, job_id, action, log):
        status, code = 'FAILED', 1
        env = {**os.environ, 'PYTHONPATH': str(self.root / 'src') + (os.pathsep + os.environ['PYTHONPATH'] if os.environ.get('PYTHONPATH') else '')}
        env.pop('OPENAI_API_KEY', None)
        env.pop('COINBASE_API_KEY', None)
        try:
            if action == 'l2_capture':
                (self.root / 'private_data' / 'market').mkdir(parents=True, exist_ok=True)
            argv = self._argv(action, job_id)
            sequences = [argv] if argv is not None else [
                [sys.executable, '-m', 'alpha_oto', 'data-update-coinbase', '--csv', s]
                for s in ('private_data/BTC-USD_1h.csv', 'private_data/ETH-USD_1h.csv')]
            with log.open('w', encoding='utf-8') as out:
                for command in sequences:
                    out.write('Action: '+action+' | Started '+_utc()+'\n')
                    out.flush()
                    proc = subprocess.run(command, cwd=self.root, env=env,
                                          stdout=out, stderr=subprocess.STDOUT,
                                          timeout=self.ACTIONS[action]['timeout'], check=False)
                    code = proc.returncode
                    if code:
                        out.write(f'\nStopped with exit code {code}\n')
                        break
                status = 'SUCCEEDED' if code == 0 else 'FAILED'
                if code == 0 and action == 'paper_start':
                    # Background future-only monitoring while the dashboard lives.
                    self.set_schedule(True)
        except subprocess.TimeoutExpired:
            status, code = 'TIMED_OUT', 124
            with log.open('a', encoding='utf-8') as out:
                out.write('\nOperation exceeded fixed maximum duration\n')
        except Exception as exc:
            with log.open('a', encoding='utf-8') as out:
                out.write(f'\nOperation failed safely: {type(exc).__name__}\n')
        finally:
            with self._lock:
                with self._db() as db:
                    db.execute('UPDATE jobs SET status=?,exit_code=?,ended_at=? WHERE id=?',
                               (status, code, _utc(), job_id))
                    db.commit()

    def set_schedule(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('Expected enabled boolean')
        if enabled and not (self.root / 'private_data/forward/forward_experiment.json').is_file():
            raise ValueError('Freeze a forward experiment with the CLI before starting the hourly paper monitor')
        with self._lock:
            self._put('schedule_enabled', enabled)
        return {'enabled': enabled, 'mode': 'FORWARD_PAPER_ONLY', 'requires_running_app': True}

    def _scheduler_loop(self):
        # Bounded polling while the server is alive. Never starts a real order.
        while not self._stop.wait(30):
            if not self._get('schedule_enabled', False):
                continue
            # Run at :02-:09 UTC after a newly completed market hour. Treat
            # missing runs as skipped; forward_lab forbids retroactive fills.
            now = datetime.now(timezone.utc)
            if not 2 <= now.minute < 10:
                continue
            stamp = now.strftime('%Y%m%d%H')
            with self._lock:
                if self._get('last_scheduled_hour') == stamp:
                    continue
                self._put('last_scheduled_hour', stamp)
                try:
                    self.launch('forward')
                except RuntimeError:
                    # Do not queue another attempt or backfill a signal.
                    pass

    def overview(self):
        setup = self.setup()
        series = []
        for symbol in ('BTC-USD', 'ETH-USD'):
            file = self.root / 'private_data' / f'{symbol}_1h.csv'
            series.append({'symbol': symbol, 'present': file.exists(),
                           'size_bytes': file.stat().st_size if file.exists() else None,
                           'last_record': _tail(file) if file.exists() else None})
        fpath = self.root / 'private_data/forward/forward_experiment.json'
        if fpath.is_file():
            try:
                from .forward_lab import status_forward
                forward = status_forward(fpath)
            except Exception:
                forward = {'evidence_state': 'FORWARD_INTEGRITY_REVIEW_REQUIRED'}
        else:
            forward = {'evidence_state': 'NOT_FROZEN'}
        reports = {}
        for key, path in {
            'tournament': 'artifacts/omega/quant_tournament.json',
            'ml': 'artifacts/omega/cross_ml/cross_asset_research.json',
            'stress': 'artifacts/omega/stress.json',
            'swarm': 'artifacts/omega/shadow_swarm.json',
        }.items():
            payload = _json_file(self.root / path)
            if isinstance(payload, dict):
                reports[key] = {'present': True, 'status': payload.get('status'),
                                'selected': payload.get('selected'),
                                'holdout': str(payload.get('holdout', ''))[:170]}
            else:
                reports[key] = {'present': False}
        capture_dir = self.root / 'private_data/market'
        market_files = sorted(capture_dir.glob('dashboard_*.jsonl'), reverse=True) if capture_dir.exists() else []
        return {'product': 'ALPHA / OTO', 'mode': 'LOCAL_PAPER_ONLY',
                'live_approved': False, 'broker_connected': self.dhan.status()['connected'],
                'payments_supported': False, 'clock_utc': _utc(),
                'series': series, 'forward': forward, 'reports': reports,
                'setup': setup, 'broker_readonly': self.dhan.status(), 'jobs': self.jobs(6),
                'market_captures': [{'name': f.name, 'bytes': f.stat().st_size} for f in market_files[:4]],
                'disk_free_gb': round(shutil.disk_usage(self.root).free / 1e9, 1),
                'runtime': sys.version.split()[0]}

    def close(self):
        self.dhan.disconnect()
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)


def make_handler(service, *, secret):
    csrf = secrets.token_urlsafe(24)
    cookie_name = 'alpha_oto_local'

    class Handler(BaseHTTPRequestHandler):
        server_version = 'AlphaOTO-Local/1.0'
        def log_message(self, fmt, *args):
            # Avoid logging login credentials or operator token to stdout.
            if '/?login=' not in self.path:
                super().log_message(fmt, *args)

        def _base(self, status=200, mime='application/json; charset=utf-8', length=0, *, headers=None):
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(length))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def _send(self, obj, status=200):
            data = json.dumps(obj, default=str, allow_nan=False).encode('utf-8')
            self._base(status, length=len(data))
            self.wfile.write(data)

        def _authenticated(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get('Cookie', ''))
            except Exception:
                return False
            item = cookie.get(cookie_name)
            return bool(item and hmac.compare_digest(item.value, secret))

        def _auth(self):
            if not self._authenticated():
                self._send({'error': 'Open the local launch URL printed in Terminal to authorize your browser.'}, 401)
                return False
            return True

        def _host_allowed(self):
            host=self.headers.get('Host', '')
            return host in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}

        def do_GET(self):
            if not self._host_allowed():
                return self._send({'error':'Localhost origin required'},403)
            uri = urlsplit(self.path)
            if uri.path == '/' and 'login' in parse_qs(uri.query):
                value = parse_qs(uri.query)['login'][0]
                if not hmac.compare_digest(value, secret):
                    self._send({'error': 'Invalid local launch token'}, 401)
                    return
                self._base(303, mime='text/plain', headers={
                    'Location': '/',
                    'Set-Cookie': f'{cookie_name}={secret}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800',
                })
                return
            if not self._auth():
                return
            if uri.path == '/api/bootstrap':
                return self._send({'csrf': csrf, 'actions': service.ACTIONS, 'safety': 'NO_LIVE_ORDERS'})
            if uri.path == '/api/overview':
                return self._send(service.overview())
            if uri.path == '/api/setup':
                return self._send(service.setup())
            if uri.path == '/api/broker/status':
                return self._send(service.dhan.status())
            if uri.path == '/api/jobs':
                return self._send({'jobs': service.jobs()})
            if uri.path == '/api/job':
                job_id = parse_qs(uri.query).get('id', [''])[0]
                try:
                    return self._send(service.job(job_id))
                except ValueError as e:
                    return self._send({'error': str(e)}, 404)
            static_map = {'/': ('index.html', 'text/html; charset=utf-8'),
                          '/app.js': ('app.js', 'application/javascript; charset=utf-8'),
                          '/styles.css': ('styles.css', 'text/css; charset=utf-8')}
            if uri.path not in static_map:
                return self._send({'error': 'Not found'}, 404)
            filename, mime = static_map[uri.path]
            data = (STATIC / filename).read_bytes()
            self._base(200, mime=mime, length=len(data))
            self.wfile.write(data)

        def do_POST(self):
            if not self._host_allowed():
                return self._send({'error':'Localhost origin required'},403)
            if not self._auth():
                return
            origin = self.headers.get('Origin', '')
            host = self.headers.get('Host', '')
            if (not origin or origin != f'http://{host}' or
                    self.headers.get('X-Alpha-CSRF') != csrf or
                    self.headers.get('Content-Type', '').split(';')[0] != 'application/json'):
                return self._send({'error': 'Origin, JSON content type, or CSRF validation failed'}, 403)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 1 <= size <= MAX_BODY:
                    raise ValueError('Invalid input size')
                raw = json.loads(self.rfile.read(size))
                if not isinstance(raw, dict):
                    raise ValueError('Expected JSON object')
                if self.path == '/api/setup':
                    return self._send(service.save_setup(raw))
                if self.path == '/api/broker/connect':
                    if set(raw) != {'client_id', 'token'}:
                        raise ValueError('Only Dhan client ID and access token allowed')
                    return self._send(service.dhan.connect(raw['token'], raw['client_id']))
                if self.path == '/api/broker/disconnect':
                    if raw:
                        raise ValueError('Disconnect has no parameters')
                    return self._send(service.dhan.disconnect())
                if self.path == '/api/broker/snapshot':
                    if raw:
                        raise ValueError('Read-only snapshot has no parameters')
                    return self._send(service.dhan.snapshot())
                if self.path == '/api/jobs':
                    if set(raw) != {'action'}:
                        raise ValueError('Only a named allowlisted operation is permitted')
                    return self._send(service.launch(raw['action']), 202)
                if self.path == '/api/scheduler':
                    if set(raw) != {'enabled'}:
                        raise ValueError('Only a paper-scheduler toggle is permitted')
                    return self._send(service.set_schedule(raw['enabled']))
                return self._send({'error': LIVE_REJECTION}, 404)
            except RuntimeError as e:
                return self._send({'error': str(e)}, 409)
            except (ValueError, KeyError, json.JSONDecodeError) as e:
                return self._send({'error': str(e)}, 400)

    return Handler


def serve(root=ROOT, *, port=8765, open_browser=True):
    service = DashboardService(root)
    secret = secrets.token_urlsafe(32)
    try:
        handler = make_handler(service, secret=secret)
        server = ThreadingHTTPServer(('127.0.0.1', port), handler)
        server.daemon_threads = True
        addr = server.server_address[1]
        url = f'http://127.0.0.1:{addr}/?login={secret}'
        print('Alpha_OTO local operator dashboard', flush=True)
        print(f'Open this private local URL: {url}', flush=True)
        print('Dhan read-only account inspection available. Live orders, deposits and payments disabled.', flush=True)
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever(poll_interval=.2)
        finally:
            server.server_close()
    finally:
        service.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Secure loopback Alpha_OTO GUI, paper-only')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-open', action='store_true')
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error('Port must be 0..65535')
    serve(port=args.port, open_browser=not args.no_open)


if __name__ == '__main__':
    main()
