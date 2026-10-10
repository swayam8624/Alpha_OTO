"""GET-only broker integration safety tests; no Dhan network calls."""
from __future__ import annotations
import io
import json
import tempfile
from pathlib import Path
import unittest
from urllib.error import HTTPError
from alpha_oto.dhan_readonly import DhanReadOnlyClient, DhanSession, DhanConnectionError
from alpha_oto.dashboard import DashboardService

TOKEN = 'a' * 48 + '.test.token'
ACCOUNT = '1000000001'

class Reply(io.BytesIO):
    def __init__(self, data):super().__init__(json.dumps(data).encode())

class BrokerStub:
    def __init__(self):
        self.paths=[]
        self.responses={
            '/profile': {'dhanClientId': ACCOUNT, 'activeSegment': 'Equity', 'tokenValidity':'11/10/2026 10:00'},
            '/fundlimit': {'dhanClientId': ACCOUNT,'availabelBalance': 1520.50, 'utilizedAmount': 95, 'withdrawableBalance': 1480},
            '/holdings': [{'tradingSymbol':'TCS','totalQty':3,'availableQty':3,'avgCostPrice':3100,'securityId':'11536'}],
            '/positions': [{'tradingSymbol':'INFY','netQty':2,'productType':'CNC','securityId':'1123'}],
            '/orders': [{'tradingSymbol':'TCS','orderStatus':'TRADED','transactionType':'BUY','quantity':3}]
        }
    def __call__(self,req, timeout):
        self.paths.append((req.full_url, req.get_method(), req.get_header('Access-token'), timeout))
        return Reply(self.responses[req.full_url.removeprefix('https://api.dhan.co/v2')])

class DhanBrokerTests(unittest.TestCase):
    def setUp(self):
        self.stub=BrokerStub()
        self.s=DhanSession(DhanReadOnlyClient(opener=self.stub))

    def test_validation_and_memory_only_connect(self):
        self.assertFalse(self.s.status()['connected'])
        connected=self.s.connect(TOKEN, ACCOUNT)
        self.assertTrue(connected['connected'])
        self.assertNotIn(TOKEN, str(connected))
        self.assertEqual(connected['client_id_masked'], '••••0001')
        self.assertFalse(connected['live_orders_enabled'])
        self.assertEqual([x[1] for x in self.stub.paths], ['GET'])
        self.assertEqual(self.stub.paths[0][2], TOKEN)
        self.assertEqual(self.stub.paths[0][0], 'https://api.dhan.co/v2/profile')

    def test_snapshot_is_all_get_and_redacted(self):
        self.s.connect(TOKEN,ACCOUNT)
        snapshot=self.s.snapshot()
        self.assertEqual(snapshot['available_balance'],'1520.5')
        self.assertEqual(snapshot['holdings']['rows'][0]['symbol'],'TCS')
        self.assertEqual(snapshot['orders']['total'],1)
        self.assertFalse(snapshot['live_orders_enabled'])
        self.assertNotIn(TOKEN,json.dumps(snapshot))
        self.assertEqual([x[1] for x in self.stub.paths], ['GET']*5)
        self.assertEqual([x[0].split('/v2')[1] for x in self.stub.paths],
                         ['/profile','/fundlimit','/holdings','/positions','/orders'])

    def test_no_other_routes_even_with_trading_token(self):
        for path in ('/orders/1','/positions/convert','/fundlimit?transfer=true','/withdraw',
                     'https://evil.example/steal','/orders/../profile'):
            with self.subTest(path=path),self.assertRaises(DhanConnectionError):
                self.s.client.fetch(path,TOKEN)
        self.assertFalse(self.stub.paths)

    def test_wrong_account_never_retains_credential(self):
        with self.assertRaises(DhanConnectionError):self.s.connect(TOKEN,'1000000002')
        self.assertFalse(self.s.status()['connected'])
        with self.assertRaises(DhanConnectionError):self.s.snapshot()

    def test_malformed_token_never_sent(self):
        for value in ('', 'short','a'*50+'\r\nX-Header:bad','a'*4097, 12):
            with self.subTest(value=str(value)[:15]),self.assertRaises(DhanConnectionError):
                self.s.connect(value,ACCOUNT)
        self.assertFalse(self.stub.paths)

    def test_disconnect_clears_in_memory_session(self):
        self.s.connect(TOKEN,ACCOUNT)
        self.assertFalse(self.s.disconnect()['connected'])
        with self.assertRaises(DhanConnectionError):self.s.snapshot()

    def test_empty_or_large_and_invalid_remote_data_fail_closed(self):
        def oversized(req,timeout):return io.BytesIO(b'X' * 1000010)
        with self.assertRaises(DhanConnectionError):DhanReadOnlyClient(opener=oversized).fetch('/profile',TOKEN)
        def broken(req,timeout):return io.BytesIO(b'<html>not json</html>')
        with self.assertRaises(DhanConnectionError):DhanReadOnlyClient(opener=broken).fetch('/profile',TOKEN)

    def test_http_error_does_not_include_secret(self):
        def bad(req,timeout):
            raise HTTPError(req.full_url,401,'access-token: '+TOKEN,{},io.BytesIO(TOKEN.encode()))
        with self.assertRaises(DhanConnectionError) as context:
            DhanReadOnlyClient(opener=bad).fetch('/profile',TOKEN)
        self.assertNotIn(TOKEN,str(context.exception))

    def test_expired_session_clears_token(self):
        self.s.connect(TOKEN,ACCOUNT)
        def expired(req,timeout):raise HTTPError(req.full_url,403,'token',{},None)
        self.s.client.opener=expired
        with self.assertRaises(DhanConnectionError):self.s.snapshot()
        self.assertFalse(self.s.status()['connected'])

    def test_remote_html_in_symbol_is_not_interpreted_by_backend(self):
        self.stub.responses['/holdings'][0]['tradingSymbol']='<script>alert(1)</script>'
        self.s.connect(TOKEN,ACCOUNT)
        self.assertEqual(self.s.snapshot()['holdings']['rows'][0]['symbol'],'<script>alert(1)</script>')
        # JS escapes all untrusted strings through escapeHTML before inserting HTML.

    def test_holdings_list_capped(self):
        self.stub.responses['/holdings']=[{'tradingSymbol':str(i)} for i in range(123)]
        self.s.connect(TOKEN,ACCOUNT)
        snap=self.s.snapshot()['holdings']
        self.assertEqual(snap['total'],123)
        self.assertEqual(len(snap['rows']),50)
        self.assertTrue(snap['truncated'])

    def test_disconnect_on_application_shutdown_and_no_token_in_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            operator=DashboardService(root=tmp,start_scheduler=False)
            operator.dhan.client=self.s.client
            operator.dhan.connect(TOKEN,ACCOUNT)
            self.assertTrue(operator.overview()['broker_connected'])
            self.assertNotIn(TOKEN,(Path(tmp)/'private_data/dashboard/operator.sqlite3').read_bytes().decode('latin1'))
            operator.close()
            self.assertFalse(operator.dhan.status()['connected'])

if __name__=='__main__':unittest.main()

class BrowserSafetyAndPaperTests(unittest.TestCase):
    def test_paper_start_command_is_allowlisted_and_no_broker_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            service=DashboardService(root=tmp,start_scheduler=False)
            try:
                self.assertIn('paper_start',service.ACTIONS)
                argv=service._argv('paper_start','fake-id')
                self.assertEqual(argv,['bash','scripts/start_paper_once.sh'])
                self.assertNotIn('orders',str(argv))
                self.assertFalse(service.setup()['real_orders_enabled'])
            finally: service.close()

    def test_refuse_browser_setup_as_credentials_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            service=DashboardService(root=tmp,start_scheduler=False)
            try:
                with self.assertRaises(ValueError):
                    service.save_setup({'market':'india_cash_equities','broker':'dhan','paper_budget':5000,'token':TOKEN})
                self.assertNotIn(TOKEN,(Path(tmp)/'private_data/dashboard/operator.sqlite3').read_bytes().decode('latin1'))
            finally:service.close()
