"""Bounded automatic navigation regressions, using only local HTTP fixtures.

Set SBS_TEST_RUNTIME_INSTALL to an existing runtime with lib/runtime-config.json.
This suite never installs software, invokes Codex, or contacts a public site.
The skill assets are copied into disposable directories by test-url-budget.py.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import unittest
import http.server

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('sbs_budget_tests', HERE / 'test-url-budget.py')
budget_tests = importlib.util.module_from_spec(spec)
spec.loader.exec_module(budget_tests)
ASSETS, NODE, RUNTIME = budget_tests.ASSETS, budget_tests.NODE, budget_tests.RUNTIME

UNIT_CODE = r"""
import assert from 'node:assert/strict';
const {NavigationTransaction,NAVIGATION_LIMITS}=await import(process.argv[1]);
let at=0;const make=(url='https://example.test/a',original=true)=>new NavigationTransaction(url,{original,allowedOrigins:['https://example.test'],remaining:180000,now:()=>at});
const results=[];function test(name,fn){if(name!==process.argv[2])return;at=0;fn();results.push({name,ok:true});}
test('http redirect',()=>{const t=make();t.accept({id:'1',url:t.url});t.response('1',t.url,302);t.accept({id:'2',url:'https://sso.test/b'});t.response('2','https://sso.test/b',200);t.domReady();at=750;assert(t.settled('https://sso.test/b'));assert.equal(t.finish('https://sso.test/b').automatic_transitions,1);});
test('automatic form POST and script return',()=>{const t=make();t.accept({id:'1',url:t.url});t.response('1',t.url,200);t.accept({id:'2',url:'https://sso.test/install?uuid=SECRET',method:'POST'});t.response('2','https://sso.test/install?uuid=SECRET',200);t.accept({id:'3',url:t.url+'?q=PRIVATE',initiator:'script'});t.response('3',t.url+'?q=PRIVATE',200);t.domReady();at=750;assert(t.settled(t.url+'?q=PRIVATE'));assert(!JSON.stringify(t.finish(t.url+'?q=PRIVATE')).includes('PRIVATE'));assert(!JSON.stringify(t.snapshot()).includes('SECRET'));});
test('hop limit includes client-side loop',()=>{const t=make();for(let i=0;i<=10;i++)t.accept({id:String(i),url:i%2?'https://example.test/b':t.url});assert.throws(()=>t.accept({id:'11',url:'https://example.test/b'}),/transition limit/);});
test('absolute deadline cannot reset',()=>{const t=make();t.accept({id:'1',url:t.url});at=29000;t.accept({id:'2',url:'https://example.test/b'});at=30000;assert.throws(()=>t.timeout(),/deadline/);});
test('parent deadline stays shorter',()=>{const t=new NavigationTransaction('https://example.test/a',{original:true,remaining:250,now:()=>at});t.accept({id:'1',url:t.url});at=250;assert.throws(()=>t.timeout(),/deadline/);});
test('internal redirect cannot extend origins',()=>{const t=make('https://example.test/a',false);t.accept({id:'1',url:t.url});assert.throws(()=>t.accept({id:'2',url:'https://other.test/a'}),/outside observed site/);});
test('initial request must match explicit assignment',()=>{const t=make();assert.throws(()=>t.accept({id:'1',url:'https://other.test/a'}),/differs/);});
test('no unsupported scheme',()=>{assert.throws(()=>make('file:///etc/passwd'),/HTTP/);});
test('no credentials in navigation URLs',()=>{assert.throws(()=>make('https://user:password@example.test/a'),/credential/);});
test('completed transaction cannot be reused',()=>{const t=make();t.accept({id:'1',url:t.url});t.response('1',t.url,200);t.domReady();at=750;t.finish(t.url);assert.throws(()=>t.accept({id:'2',url:t.url}),/closed/);});
console.log(JSON.stringify({ok:true,limits:NAVIGATION_LIMITS,tests:results},null,2));
"""

UNIT_CASES = [
    'http redirect', 'automatic form POST and script return',
    'hop limit includes client-side loop', 'absolute deadline cannot reset',
    'parent deadline stays shorter', 'internal redirect cannot extend origins',
    'initial request must match explicit assignment', 'no unsupported scheme',
    'no credentials in navigation URLs', 'completed transaction cannot be reused',
]
class NavigationUnitTests(unittest.TestCase):
    def run_case(self, name):
        result = subprocess.run([NODE, '--input-type=module', '-e', UNIT_CODE,
            (ASSETS / 'sbs-navigation-transaction.mjs').as_uri(), name],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['tests'], [{'name': name, 'ok': True}])
for name in UNIT_CASES:
    def run(self, case=name):
        self.run_case(case)
    setattr(NavigationUnitTests, 'test_' + name.replace(' ', '_').replace('-', '_'), run)

class ForeignHandler(http.server.BaseHTTPRequestHandler):
    hits = 0
    def log_message(self, *args):
        pass
    def do_GET(self):
        type(self).hits += 1
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'FORBIDDEN_CONTACT')
    do_POST = do_GET

class NavigationHandler(http.server.BaseHTTPRequestHandler):
    requests = []
    foreign_url = None
    delay_response = False
    release_response = threading.Event()
    def log_message(self, *args):
        pass
    def do_GET(self):
        route = self.path.split('?')[0]
        type(self).requests.append((self.command, route))
        if route == '/delayed' and type(self).delay_response:
            type(self).release_response.wait(timeout=40)
            raw = b'<body>LATE_RESPONSE_MUST_NOT_COMMIT</body>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if route in ['/http', '/internal-redirect']:
            self.send_response(302)
            self.send_header('Location', '/final' if route == '/http' else self.foreign_url)
            self.end_headers()
            return
        if route == '/stall':
            # Keep the response pending until Chromium aborts the timed-out goto.
            self.connection.settimeout(40)
            try:
                self.rfile.read(1)
            except (OSError, TimeoutError):
                pass
            return
        if route == '/js':
            body = '<script>location.replace("/final")</script>'
        elif route == '/meta':
            body = '<meta http-equiv="refresh" content="0; url=/final">'
        elif route == '/form':
            body = '<form method="POST" action="/final"></form><script>document.querySelector("form").submit()</script>'
        elif route.startswith('/loop'):
            body = '<script>location.replace("' + ('/loop-b' if route == '/loop-a' else '/loop-a') + '")</script>'
        elif route == '/late':
            body = '<body>LATE_ORIGINAL<script>setTimeout(()=>location.replace("' + self.foreign_url + '"),1800)</script>'
        else:
            body = '<html><title>Fixture</title><body>' + ('SBS_FINAL_CONTENT' if route == '/final' else 'CONTENT ' + route)
            body += ''.join('<a href="/p%d">P%d</a>' % (i, i) for i in range(1, 5))
            body += '<a href="/internal-redirect">Redirect</a><a href="' + self.foreign_url + '">Foreign</a></body></html>'
        raw = body.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass
    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', '0')))
        self.do_GET()

@unittest.skipUnless((RUNTIME / 'lib/runtime-config.json').exists(), 'Pinned Playwright runtime fixture unavailable')
class NavigationBrowserTests(unittest.TestCase):
    setUp = budget_tests.BrowserTests.setUp
    tearDown = budget_tests.BrowserTests.tearDown
    statefile = budget_tests.BrowserTests.statefile
    def command(self, url, mode='--stdio'):
        return budget_tests.BrowserTests.command(self, url, mode) + ['--android']
    start = budget_tests.BrowserTests.start
    close = budget_tests.BrowserTests.close
    assert_child_gone = budget_tests.BrowserTests.assert_child_gone

    def action(self, process, **command):
        process.stdin.write(json.dumps(command) + '\n')
        process.stdin.flush()
        return process.replies.get(timeout=45)

    @classmethod
    def setUpClass(cls):
        cls.foreign = http.server.ThreadingHTTPServer(('127.0.0.1', 0), ForeignHandler)
        threading.Thread(target=cls.foreign.serve_forever, daemon=True).start()
        cls.foreign_url = f'http://127.0.0.1:{cls.foreign.server_port}/foreign'
        NavigationHandler.foreign_url = cls.foreign_url
        cls.http = http.server.ThreadingHTTPServer(('127.0.0.1', 0), NavigationHandler)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.http.server_port}'

    @classmethod
    def tearDownClass(cls):
        for server in [cls.http, cls.foreign]:
            server.shutdown()
            server.server_close()

    def root_session(self):
        url = self.base + '/root'
        process = self.start(url)
        self.assertTrue(self.action(process, op='goto', url=url)['ok'])
        return process

    def guard_failure(self, reply, message):
        self.assertFalse(reply['ok'], reply)
        self.assertIn(message, reply['error'])
        self.assertEqual(reply['error_source'], 'runtime_navigation_guard', reply)

    def automatic_chain(self, endpoint):
        url = self.base + '/' + endpoint
        process = self.start(url)
        reply = self.action(process, op='goto', url=url)
        self.assertTrue(reply['ok'], reply)
        self.assertIn('SBS_FINAL_CONTENT', reply['text'])
        self.assertEqual(reply['http_status'], 200)
        self.assertEqual(reply['navigation']['automatic_transitions'], 1)
        self.assertTrue(reply['navigation']['completed'])
        self.assertEqual(len(reply['budget']['internal_pages']), 0)
        if endpoint == 'form':
            self.assertEqual(reply['navigation']['hops'][1]['method'], 'POST')
        self.close(process)

    def test_http_chain(self):
        self.automatic_chain('http')
    def test_js_chain(self):
        self.automatic_chain('js')
    def test_meta_chain(self):
        self.automatic_chain('meta')
    def test_automatic_form_chain(self):
        self.automatic_chain('form')

    def test_transition_limit_denies_eleventh_transition_before_contact(self):
        process = self.start(self.base + '/loop-a')
        before = len(NavigationHandler.requests)
        reply = self.action(process, op='goto', url=self.base + '/loop-a')
        self.guard_failure(reply, 'transition limit')
        self.assertLessEqual(len(NavigationHandler.requests) - before, 11)
        self.assertEqual(reply['navigation']['automatic_transitions'], 10)
        self.close(process)

    def test_late_external_navigation_denied_before_contact(self):
        url = self.base + '/late'
        process = self.start(url)
        self.assertTrue(self.action(process, op='goto', url=url)['ok'])
        before = ForeignHandler.hits
        reply = self.action(process, op='wait', ms=2000)
        self.guard_failure(reply, 'purpose')
        self.assertEqual(ForeignHandler.hits, before)
        self.close(process)

    def test_internal_page_limit(self):
        process = self.root_session()
        for i in range(1, 4):
            reply = self.action(process, op='goto', url=self.base + f'/p{i}', purpose=f'Read missing detail {i}')
            self.assertTrue(reply['ok'], reply)
        reply = self.action(process, op='goto', url=self.base + '/p4', purpose='Fourth detail')
        self.guard_failure(reply, 'page limit')
        self.assertEqual(len(json.loads(self.statefile(self.base + '/root').read_text())['internal_pages']), 3)
        self.close(process)

    def test_missing_purpose_denied_before_contact(self):
        process = self.root_session()
        before = len(NavigationHandler.requests)
        reply = self.action(process, op='goto', url=self.base + '/p1')
        self.guard_failure(reply, 'purpose')
        self.assertEqual(len(NavigationHandler.requests), before)
        self.close(process)

    def test_unobserved_link_denied_before_contact(self):
        process = self.root_session()
        before = len(NavigationHandler.requests)
        reply = self.action(process, op='goto', url=self.base + '/unseen', purpose='detail')
        self.guard_failure(reply, 'observed link')
        self.assertEqual(len(NavigationHandler.requests), before)
        self.close(process)

    def test_external_link_denied_before_contact(self):
        process = self.root_session()
        before = ForeignHandler.hits
        reply = self.action(process, op='goto', url=self.foreign_url, purpose='detail')
        self.guard_failure(reply, 'outside')
        self.assertEqual(ForeignHandler.hits, before)
        self.close(process)

    def test_internal_external_redirect_denied_before_contact(self):
        process = self.root_session()
        before = ForeignHandler.hits
        reply = self.action(process, op='goto', url=self.base + '/internal-redirect', purpose='detail')
        self.guard_failure(reply, 'outside')
        self.assertEqual(ForeignHandler.hits, before)
        self.assertEqual(len(json.loads(self.statefile(self.base + '/root').read_text())['internal_pages']), 1)
        self.close(process)

    def test_stalled_goto_has_absolute_navigation_deadline(self):
        url = self.base + '/stall'
        process = self.start(url)
        before = time.monotonic()
        reply = self.action(process, op='goto', url=url)
        elapsed = time.monotonic() - before
        self.guard_failure(reply, 'deadline')
        self.assertGreaterEqual(elapsed, 28)
        self.assertLess(elapsed, 33)
        self.close(process)

    def test_delayed_initial_response_cannot_commit_after_timeout(self):
        url = self.base + '/delayed'
        process = self.start(url)
        self.assertTrue(self.action(process, op='goto', url=url)['ok'])
        NavigationHandler.release_response.clear()
        NavigationHandler.delay_response = True
        try:
            reply = self.action(process, op='goto', url=url)
            self.guard_failure(reply, 'deadline')
            self.assertFalse(reply['navigation']['completed'])
            NavigationHandler.release_response.set()
            time.sleep(0.3)
            observed = self.action(process, op='observe')
            self.assertTrue(observed['ok'], observed)
            self.assertIn('CONTENT /delayed', observed['text'])
            self.assertNotIn('LATE_RESPONSE_MUST_NOT_COMMIT', observed['text'])
            self.assertEqual(observed['budget']['reloads'], 1)
        finally:
            NavigationHandler.delay_response = False
            NavigationHandler.release_response.set()
            self.close(process)

if __name__ == '__main__':
    unittest.main(verbosity=2)
