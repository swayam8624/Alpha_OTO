"""Operator dashboard security, durability, and paper-only integration tests."""
import http.cookiejar
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import build_opener, HTTPCookieProcessor, Request
from http.server import ThreadingHTTPServer

from alpha_oto.dashboard import DashboardService, make_handler, LIVE_REJECTION


class DashboardPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service = DashboardService(self.root, start_scheduler=False)
        self.addCleanup(self.service.close)

    def test_default_modes_never_include_live(self):
        d = self.service.overview()
        self.assertFalse(d['live_approved'])
        self.assertFalse(d['broker_connected'])
        self.assertFalse(d['payments_supported'])
        self.assertEqual(d['mode'], 'LOCAL_PAPER_ONLY')

    def test_onboarding_is_persistent_and_cannot_connect_broker(self):
        self.service.save_setup({'market': 'india_cash_equities', 'broker': 'dhan', 'paper_budget': 25000})
        other = DashboardService(self.root, start_scheduler=False)
        self.addCleanup(other.close)
        self.assertEqual(other.setup()['market'], 'india_cash_equities')
        self.assertEqual(other.setup()['paper_budget'], 25000.0)
        self.assertFalse(other.setup()['broker_connected'])
        self.assertFalse(other.setup()['payments_connected'])
        self.assertFalse(other.setup()['real_orders_enabled'])

    def test_payment_and_secret_fields_are_rejected(self):
        for key in ('bank_account','api_key','payment_token','live_enabled'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.service.save_setup({'market':'crypto_spot','broker':'other','paper_budget':1000,key:'secret'})
        self.assertEqual(self.service.setup()['broker'],'undecided')

    def test_bad_budget_rejected(self):
        for v in (float('nan'), True, '5', 0, -10, 100000000):
            with self.subTest(v=v), self.assertRaises(ValueError):
                self.service.save_setup({'market':'undecided','broker':'undecided','paper_budget':v})

    def test_unlisted_actions_cannot_start_orders_or_shell(self):
        for val in ('live-order','deposit','connect-broker','python -c print(1)',None,[]):
            with self.subTest(val=val), self.assertRaises(ValueError):
                self.service.launch(val)
        self.assertFalse(self.service.jobs())

    def test_scheduler_requires_actual_frozen_forward_experiment(self):
        with self.assertRaises(ValueError):
            self.service.set_schedule(True)
        self.assertFalse(self.service.setup()['paper_schedule_enabled'])
        file = self.root / 'private_data/forward/forward_experiment.json'
        file.parent.mkdir(parents=True)
        file.write_text('{}')
        self.assertTrue(self.service.set_schedule(True)['enabled'])
        self.service.set_schedule(False)
        self.assertFalse(self.service.setup()['paper_schedule_enabled'])

    def test_only_one_job_can_run_at_once_and_terminal_state_persists(self):
        # The test suite in this small fake workspace finishes with failure,
        # proving that reported errors are not falsely marked success.
        a = self.service.launch('tests')
        with self.assertRaises(RuntimeError):
            self.service.launch('omega')
        deadline = time.time()+15
        while time.time()<deadline:
            job=self.service.job(a['job_id'])
            if job['status']!='RUNNING':break
            time.sleep(.05)
        self.assertEqual(job['status'],'FAILED')
        self.assertNotEqual(job['exit_code'],0)
        self.assertIn('Action: tests',job['output'])
        reopened = DashboardService(self.root,start_scheduler=False)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.job(a['job_id'])['status'],'FAILED')

    def test_running_job_after_restart_is_interrupted(self):
        with self.service._db() as db:
            db.execute('INSERT INTO jobs(id,action,status,started_at,log_path) VALUES(?,?,?,?,?)',
                       ('lost-job','tests','RUNNING','2026-01-01',str(self.service.jobs_dir/'lost-job.log')))
        reopened=DashboardService(self.root,start_scheduler=False)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.jobs()[0]['status'],'INTERRUPTED')

    def test_symlink_log_is_never_read(self):
        secret=self.root/'private_data/private.txt'
        secret.write_text('SENSITIVE DO NOT READ')
        link=self.service.jobs_dir/'somejob.log'
        link.symlink_to(secret)
        with self.service._db() as db:
            db.execute('INSERT INTO jobs(id,action,status,started_at,log_path) VALUES(?,?,?,?,?)',
                       ('somejob','tests','FAILED','2026-01-01',str(link)))
        with self.assertRaises(ValueError):
            self.service.job('somejob')


class DashboardHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.service=DashboardService(self.tmp.name,start_scheduler=False)
        self.addCleanup(self.service.close)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.service,secret='a-private-launch-secret'))
        self.addCleanup(self.server.server_close)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.base='http://127.0.0.1:'+str(self.server.server_port)
        self.browser=build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def _request(self,path,*,body=None,csrf=None,origin=True):
        headers={}
        if body is not None:
            headers['Content-Type']='application/json'
            if csrf is not None:headers['X-Alpha-CSRF']=csrf
            if origin:headers['Origin']=self.base
        r=Request(self.base+path,data=json.dumps(body).encode() if body is not None else None,
                  headers=headers,method='POST' if body is not None else 'GET')
        try:
            response=self.browser.open(r,timeout=3)
            data=response.read()
            return response.status, json.loads(data) if response.headers.get('Content-Type','').startswith('application/json') else data.decode()
        except HTTPError as e:
            data=e.read()
            return e.code,json.loads(data)

    def test_credentials_required_and_login_redirect_is_local(self):
        self.assertEqual(self._request('/api/overview')[0],401)
        self.assertEqual(self._request('/?login=wrong')[0],401)
        status,html=self._request('/?login=a-private-launch-secret')
        self.assertEqual(status,200)
        self.assertIn('ALPHA',html)
        self.assertEqual(self._request('/api/overview')[0],200)

    def test_protected_post_requires_origin_and_csrf(self):
        self._request('/?login=a-private-launch-secret')
        csrf=self._request('/api/bootstrap')[1]['csrf']
        payload={'market':'crypto_spot','broker':'other','paper_budget':1000}
        self.assertEqual(self._request('/api/setup',body=payload)[0],403)
        self.assertEqual(self._request('/api/setup',body=payload,csrf=csrf,origin=False)[0],403)
        self.assertEqual(self._request('/api/setup',body=payload,csrf=csrf)[0],200)
        self.assertEqual(self._request('/api/setup')[1]['market'],'crypto_spot')
        self.assertEqual(self._request('/api/jobs',body={'action':'execute_live'},csrf=csrf)[0],400)
        self.assertEqual(self._request('/api/withdraw',body={'amount':1000},csrf=csrf)[0],404)

    def test_only_expected_static_files_exist(self):
        self._request('/?login=a-private-launch-secret')
        for path in ('/','/styles.css','/app.js'):
            self.assertEqual(self._request(path)[0],200)
        self.assertEqual(self._request('/../../etc/passwd')[0],404)
        self.assertEqual(self._request('/api/job?id=../../foo')[0],404)

if __name__=='__main__':unittest.main()

class DhanDashboardHttpTests(DashboardHttpTests):
    """Local credential flow is authenticated and CSRF-protected, never live."""

    def setUp(self):
        super().setUp()
        from alpha_oto.dhan_readonly import DhanReadOnlyClient
        from test_dhan_readonly import BrokerStub
        self.stub=BrokerStub()
        self.service.dhan.client=DhanReadOnlyClient(opener=self.stub)

    def _login_broker(self):
        self._request('/?login=a-private-launch-secret')
        self.csrf=self._request('/api/bootstrap')[1]['csrf']

    def test_broker_endpoints_reject_missing_csrf_or_secret_fields(self):
        self._login_broker()
        self.assertEqual(self._request('/api/broker/connect',body={'client_id':'1000000001','token':'a'*48})[0],403)
        self.assertEqual(self._request('/api/broker/connect',body={'client_id':'1000000001','token':'a'*48,'live_enabled':True},csrf=self.csrf)[0],400)
        self.assertEqual(self.stub.paths,[])

    def test_read_only_connect_refresh_disconnect_and_no_token_in_json(self):
        self._login_broker()
        token='a'*48
        code,body=self._request('/api/broker/connect',body={'client_id':'1000000001','token':token},csrf=self.csrf)
        self.assertEqual(code,200)
        self.assertTrue(body['connected'])
        self.assertNotIn(token,json.dumps(body))
        self.assertTrue(self._request('/api/overview')[1]['broker_connected'])
        code,body=self._request('/api/broker/snapshot',body={},csrf=self.csrf)
        self.assertEqual(code,200)
        self.assertEqual(body['holdings']['total'],1)
        self.assertNotIn(token,json.dumps(body))
        self.assertEqual(self._request('/api/broker/disconnect',body={},csrf=self.csrf)[1]['connected'],False)
        self.assertFalse(self._request('/api/overview')[1]['broker_connected'])
        self.assertEqual([p[1] for p in self.stub.paths],['GET']*5)

    def test_illegal_live_and_funding_api_routes_not_exposed(self):
        self._login_broker()
        for path in ('/api/broker/order','/api/broker/withdraw','/api/broker/pay','/api/broker/deposit'):
            self.assertEqual(self._request(path,body={'action':'BUY','amount':1},csrf=self.csrf)[0],404)
        self.assertFalse(self.service.dhan.status()['connected'])

