"""URL budgets and local HTTP/real Playwright integration; never invokes Codex.

SBS_TEST_RUNTIME_INSTALL selects an existing pinned browser runtime. All job
homes, policies, checkpoints and browser-control sockets are disposable fixtures.
"""
import base64
import hashlib
import http.server
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

ASSETS = Path(__file__).resolve().parents[1] / 'assets'
RUNTIME = Path(os.environ.get('SBS_TEST_RUNTIME_INSTALL',
    str(Path.home() / '.local/share/yatorrent-sbs')))
NODE = str(RUNTIME / 'node/bin/node') if (RUNTIME / 'node/bin/node').exists() else shutil.which('node')


class UnitTests(unittest.TestCase):
    def test_watchdog_owns_child_even_when_launch_promise_never_resolves(self):
        self.stalled_launch()

    def test_watchdog_kills_own_group_when_leader_exits_before_descendant(self):
        self.stalled_launch(descendant=True)

    def stalled_launch(self, descendant=False):
        with tempfile.TemporaryDirectory(prefix='sbs-stalled-launch-') as folder:
            root = Path(folder)
            home = root / 'job'
            home.mkdir(mode=0o700)
            lib = root / 'lib'
            lib.mkdir()
            for name in ['sbs-chromium.mjs', 'sbs-url-budget.mjs', 'sbs-navigation-transaction.mjs']:
                shutil.copyfile(ASSETS / name, lib / name)
            runtime = root / 'stub-runtime'
            package = runtime / 'node_modules/playwright'
            package.mkdir(parents=True)
            (runtime / 'package.json').write_text('{}')
            (package / 'package.json').write_text('{"main":"index.cjs","version":"fixture"}')
            child_code = "setInterval(()=>{},1000)"
            if descendant:
                child_code = """const cp=require('node:child_process'),fs=require('node:fs');
const child=cp.spawn(process.execPath,['-e','setInterval(()=>{},1000)'],{stdio:'ignore'});
fs.writeFileSync(process.env.SBS_JOB_HOME+'/fixture-descendant.json',JSON.stringify({pid:child.pid}));child.unref();process.exit(0);"""
            (package / 'index.cjs').write_text('const fixtureChildCode=' + json.dumps(child_code) + ';\n' + '''
const cp=require('node:child_process'),fs=require('node:fs');
exports.chromium={executablePath:()=>process.execPath,launchServer:async options=>{
 const child=cp.spawn(process.execPath,['-e',fixtureChildCode],{detached:true,stdio:'ignore'});
 fs.writeFileSync(process.env.SBS_JOB_HOME+'/fixture-child.json',JSON.stringify({pid:child.pid,sandbox:options.chromiumSandbox,timeout:options.timeout}));
 return new Promise(()=>{});
}};exports.devices={};
''')
            (lib / 'runtime-config.json').write_text(json.dumps({'runtime': str(runtime), 'browsers_path': str(root / 'none')}))
            url = 'http://127.0.0.1:9/stalled-launch-no-network'
            visits = home / 'url-visits'
            visits.mkdir(mode=0o700)
            statefile = visits / (hashlib.sha256(url.encode()).hexdigest() + '.json')
            now = int(time.time() * 1000)
            state = {'version': 1, 'url': url, 'started_at': now - 174000, 'saved_at': now,
                'elapsed_ms': 174000, 'captcha_encountered': False, 'captcha_elapsed_ms': 0,
                'captcha_active': False, 'captcha_choices': 0, 'internal_pages': [],
                'observed_primary_url': url, 'reloads': 0, 'initial_navigation_started': False,
                'browser_closed': True}
            statefile.write_text(json.dumps(state))
            statefile.chmod(0o600)
            target = base64.urlsafe_b64encode(url.encode()).decode().rstrip('=')
            before = time.monotonic()
            result = subprocess.run([NODE, lib / 'sbs-chromium.mjs', '--stdio', '--target64', target],
                env=dict(os.environ, SBS_JOB_HOME=str(home)), input='', capture_output=True,
                text=True, timeout=9)
            self.assertEqual(result.returncode, 124, result.stderr)
            self.assertLess(time.monotonic() - before, 7)
            child = json.loads((home / 'fixture-child.json').read_text())
            self.assertTrue(child['sandbox'])
            self.assertLessEqual(child['timeout'], 1000)
            with self.assertRaises(ProcessLookupError):
                os.kill(child['pid'], 0)
            if descendant:
                descendant_pid = json.loads((home / 'fixture-descendant.json').read_text())['pid']
                # Check the whole exact group, including the descendant whose
                # original leader has already exited. Keep failures recoverable.
                try:
                    with self.assertRaises(ProcessLookupError):
                        os.killpg(child['pid'], 0)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(descendant_pid, 0)
                finally:
                    try:
                        os.killpg(child['pid'], 9)
                    except ProcessLookupError:
                        pass
            self.assertTrue(json.loads(statefile.read_text())['browser_closed'])
            self.assertFalse(list(visits.glob('*.lock')))

    def test_budget_checkpoint_navigation_captcha_and_exclusive_lease(self):
        with tempfile.TemporaryDirectory(prefix='sbs-budget-unit-') as folder:
            os.chmod(folder, 0o700)
            code = r'''
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {UrlBudget,openBudget} from './assets/sbs-url-budget.mjs';
let now=1000000;const clock=()=>now, url='http://127.0.0.1:1234/';
const b=new UrlBudget(url,{now:clock});
assert.equal(b.remaining(),175000);assert.equal(b.timeout(5000),5000);
b.navigation(url,null,new Set());now+=5000;
const saved=b.persist(true), resumed=new UrlBudget(url,{checkpoint:saved,now:clock});
assert.equal(resumed.started,b.started);assert.equal(resumed.remaining(),170000);
resumed.navigation(url,null,new Set());assert.throws(()=>resumed.navigation(url,null,new Set()),/reload limit/);
const link=url+'failed';const links=new Set([link]);
for(let i=0;i<3;i++)resumed.navigation(link,'Check missing fact',links);
assert.throws(()=>resumed.navigation(link,'retry',links),/page limit/);
assert.throws(()=>new UrlBudget(url,{now:clock}).navigation(link,'fact',links),/original URL first/);
for(const mutation of [
  {version:2},{saved_at:NaN},{saved_at:now+1},{elapsed_ms:-1},
  {captcha_active:true,captcha_encountered:false},{captcha_elapsed_ms:120001},
  {captcha_elapsed_ms:'0'},{captcha_choices:6},{captcha_choices:1},
  {observed_primary_url:'file:///tmp/no'},{initial_navigation_started:undefined},
  {internal_pages:[{url:'http://another.invalid/one',purpose:'fact'}]},
  {internal_pages:[{url:link,purpose:''}]},
])assert.throws(()=>new UrlBudget(url,{now:clock,checkpoint:{...saved,...mutation}}));
const grant={authorized:true,grant_id:'fixture',scope:'current_url',statement:'fixture',urls:[url]};
const c=new UrlBudget(url,{now:clock});c.beginCaptcha(grant,'visible local challenge');
assert.equal(c.remaining(),295000);for(let i=0;i<5;i++)c.choice();
assert.throws(()=>c.choice(),/choice limit/);now+=60000;c.endCaptcha();
now+=1000;c.beginCaptcha(grant,'second visible challenge');now+=59000;
assert.equal(c.captchaElapsed(),119000);const active=c.persist();now+=1000;
const resumedCaptcha=new UrlBudget(url,{now:clock,checkpoint:active});
assert.throws(()=>resumedCaptcha.timeout(1),/exhausted/);
const near=new UrlBudget(url,{now:clock});now+=175000;
assert.equal(near.remaining(),0);assert.equal(near.remaining(true),5000);
assert.throws(()=>near.beginCaptcha(grant,'too late'),/exhausted/);
const lease=openBudget(url,process.argv[1]);
assert.throws(()=>openBudget(url,process.argv[1]),/already in use/);
lease.release();const again=openBudget(url,process.argv[1]);
assert.equal(again.started,lease.started);again.release();
assert.equal(fs.readdirSync(process.argv[1]+'/url-visits').filter(x=>x.endsWith('.lock')).length,0);
console.log('budget assertions passed');
'''
            result = subprocess.run([NODE, '--input-type=module', '-e', code, folder],
                cwd=ASSETS.parent, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)


class FixtureHandler(http.server.BaseHTTPRequestHandler):
    slow_calls = 0
    redirect_requests = []
    def log_message(self, *args):
        pass
    def do_GET(self):
        if self.path.startswith(('/get-301/', '/get-302/')):
            type(self).redirect_requests.append(self.path)
            prefix, step = self.path.rsplit('/', 1)
            if int(step) < 20:
                self.send_response(int(prefix.rsplit('-', 1)[1]))
                self.send_header('Location', prefix + '/' + str(int(step) + 1))
                self.end_headers()
                return
        if self.path == '/slow':
            type(self).slow_calls += 1
            if type(self).slow_calls == 1:
                time.sleep(6)
        if self.path == '/fail':
            self.connection.close()
            return
        if self.path in ['/primary', '/redirect']:
            self.send_response(302)
            self.send_header('Location', '/index' if self.path == '/primary' else '/unobserved')
            self.end_headers()
            return
        body = '''<!doctype html><body>Local budget fixture
<a id="one" href="/one">one</a><a id="fail" href="/fail">failed</a>
<a id="redirect" href="/redirect">redirect</a>
<a id="cross" href="http://localhost:9/cross">cross origin</a>
<button id="nav" onclick="location.href='/unobserved'">script navigation</button>
<a id="spa-link" href="/spa">spa path</a><button id="spa" onclick="history.pushState({},'', '/spa')">spa navigation</button>
<input id="fill-spa" oninput="history.pushState({},'', '/spa')">
<input id="fill-away" oninput="history.pushState({},'', '/unobserved');history.replaceState({},'', '/index')">
<button id="async-spa" onclick="setTimeout(()=>history.pushState({},'', '/spa'),250)">async spa</button>
<button id="async-away" onclick="setTimeout(()=>{history.pushState({},'', '/unobserved');history.replaceState({},'', '/index')},250)">async away back</button>
<button id="query-state" onclick="history.replaceState({},'', '/index?zoom=3#view')">map state</button><a href="/index?zoom=3">query navigation</a>
<a id="captcha" href="/captcha">captcha</a>'''
        if self.path == '/captcha':
            body += '<div class="h-captcha" data-sitekey="fixture">Visible challenge <button id="choice">choose</button></div>'
        raw = body.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass


@unittest.skipUnless((RUNTIME / 'lib/runtime-config.json').exists(), 'Pinned Playwright runtime fixture unavailable')
class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = http.server.ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.http.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sbs-browser-budget-')
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / 'job'
        self.home.mkdir(mode=0o700)
        self.lib = self.root / 'lib'
        self.lib.mkdir()
        for name in ['sbs-chromium.mjs', 'sbs-url-budget.mjs', 'sbs-navigation-transaction.mjs']:
            shutil.copyfile(ASSETS / name, self.lib / name)
        shutil.copyfile(RUNTIME / 'lib/runtime-config.json', self.lib / 'runtime-config.json')
        self.env = dict(os.environ, SBS_JOB_HOME=str(self.home))
        self.env.pop('NODE_TLS_REJECT_UNAUTHORIZED', None)
        self.sessions = []

    def tearDown(self):
        for process in self.sessions:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=8)
            process.stdout.close()
            process.stderr.close()
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
        self.temp.cleanup()

    def statefile(self, url):
        return self.home / 'url-visits' / (hashlib.sha256(url.encode()).hexdigest() + '.json')

    def command(self, url, mode='--stdio'):
        encoded = base64.urlsafe_b64encode(url.encode()).decode().rstrip('=')
        return [NODE, self.lib / 'sbs-chromium.mjs', mode, encoded] if mode == '--get64' else [NODE, self.lib / 'sbs-chromium.mjs', mode, '--target64', encoded]

    def start(self, url, mode='--stdio'):
        process = subprocess.Popen(self.command(url, mode), env=self.env, cwd=self.root,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.sessions.append(process)
        process.replies = queue.Queue()
        def read():
            for line in process.stdout:
                process.replies.put(json.loads(line))
        threading.Thread(target=read, daemon=True).start()
        reply = process.replies.get(timeout=15)
        self.assertTrue(reply['ok'], reply)
        self.assertEqual(reply['event'], 'ready')
        self.assertTrue(reply['spawn_process_captured'])
        self.assertTrue(reply['chromium_sandbox'])
        self.assertTrue(reply['tls_verification'])
        process.owned_browser_pid = reply['owned_browser_pid']
        process.session = reply.get('session')
        return process

    def action(self, process, **command):
        process.stdin.write(json.dumps(command) + '\n')
        process.stdin.flush()
        return process.replies.get(timeout=15)

    def close(self, process):
        reply = self.action(process, op='close')
        self.assertTrue(reply['ok'], reply)
        process.stdin.close()
        self.assertEqual(process.wait(timeout=8), 0, process.stderr.read())
        self.assert_child_gone(process)

    def assert_child_gone(self, process):
        with self.assertRaises(ProcessLookupError):
            os.kill(process.owned_browser_pid, 0)

    def test_click_redirect_unobserved_and_failure_accounting(self):
        url = self.base + '/primary'
        process = self.start(url)
        self.assertTrue(self.action(process, op='goto', url=url)['ok'])
        state = json.loads(self.statefile(url).read_text())
        self.assertEqual(state['observed_primary_url'], self.base + '/index')
        missing = self.action(process, op='click', selector='#one')
        self.assertFalse(missing['ok'], missing)
        self.action(process, op='goto', url=url)
        ok = self.action(process, op='click', selector='#one', purpose='Read missing detail')
        self.assertTrue(ok['ok'], ok)
        self.assertEqual(ok['url'], self.base + '/one')
        failed = self.action(process, op='goto', url=self.base + '/fail', purpose='Check failure')
        self.assertFalse(failed['ok'], failed)
        redirect = self.action(process, op='goto', url=self.base + '/redirect', purpose='Follow missing detail')
        # An allowed same-origin automatic chain is one attempted internal
        # page, including its final target; it needs no extra observed link.
        self.assertTrue(redirect['ok'], redirect)
        self.assertEqual(redirect['url'], self.base + '/unobserved')
        self.assertEqual(redirect['navigation']['automatic_transitions'], 1)
        state = json.loads(self.statefile(url).read_text())
        self.assertEqual(len(state['internal_pages']), 3)
        self.close(process)

    def test_spa_history_page_changes_count(self):
        url = self.base + '/index'
        process = self.start(url)
        self.action(process, op='goto', url=url)
        spa = self.action(process, op='click', selector='#spa', purpose='Read SPA missing detail')
        self.assertTrue(spa['ok'], spa)
        self.assertEqual(spa['url'], self.base + '/spa')
        self.assertEqual(len(spa['budget']['internal_pages']), 1)
        self.close(process)

    def test_fill_history_navigation_requires_purpose_and_counts(self):
        url = self.base + '/index'
        process = self.start(url)
        self.action(process, op='goto', url=url)
        missing = self.action(process, op='fill', selector='#fill-spa', value='missing purpose')
        self.assertFalse(missing['ok'], missing)
        self.close(process)
        second = self.start(self.base + '/second-fill')
        self.action(second, op='goto', url=self.base + '/second-fill')
        valid = self.action(second, op='fill', selector='#fill-spa', value='valid', purpose='Read missing SPA detail')
        self.assertTrue(valid['ok'], valid)
        self.assertEqual(len(valid['budget']['internal_pages']), 1)
        self.close(second)

    def test_fill_unobserved_away_back_cannot_hide_intermediate_navigation(self):
        url = self.base + '/index'
        process = self.start(url)
        self.action(process, op='goto', url=url)
        hidden = self.action(process, op='fill', selector='#fill-away', value='away back', purpose='Unobserved path')
        self.assertFalse(hidden['ok'], hidden)
        self.assertFalse(self.action(process, op='observe')['ok'])
        self.close(process)

    def test_wait_accounts_async_observed_navigation(self):
        url = self.base + '/index'
        process = self.start(url)
        self.action(process, op='goto', url=url)
        self.action(process, op='click', selector='#async-spa')
        waited = self.action(process, op='wait', ms=600, purpose='Read missing async SPA detail')
        self.assertTrue(waited['ok'], waited)
        self.assertEqual(len(waited['budget']['internal_pages']), 1)
        self.close(process)

    def test_wait_unobserved_async_away_back_is_sticky_violation(self):
        url = self.base + '/index'
        process = self.start(url)
        self.action(process, op='goto', url=url)
        self.action(process, op='click', selector='#async-away')
        waited = self.action(process, op='wait', ms=600, purpose='Unobserved async path')
        self.assertFalse(waited['ok'], waited)
        self.assertFalse(self.action(process, op='observe')['ok'])
        self.close(process)

    def test_map_query_history_state_keeps_page_budget_but_http_goto_counts(self):
        url = self.base + '/index'
        process = self.start(url)
        self.action(process, op='goto', url=url)
        state = self.action(process, op='click', selector='#query-state')
        self.assertTrue(state['ok'], state)
        self.assertEqual(state['state_url'], self.base + '/index?zoom=3#view')
        self.assertEqual(len(state['budget']['internal_pages']), 0)
        query_get = self.action(process, op='goto', url=self.base + '/index?zoom=3', purpose='Read explicit query result')
        self.assertTrue(query_get['ok'], query_get)
        self.assertEqual(len(query_get['budget']['internal_pages']), 1)
        self.close(process)

    def test_cross_origin_press_and_unobserved_script_navigation(self):
        url = self.base + '/index'
        process = self.start(url)
        self.action(process, op='goto', url=url)
        cross = self.action(process, op='click', selector='#cross', purpose='Cross origin is forbidden')
        self.assertFalse(cross['ok'], cross)
        self.assertEqual(len(json.loads(self.statefile(url).read_text())['internal_pages']), 0)
        self.action(process, op='goto', url=url)
        pressed = self.action(process, op='press', selector='#one', key='Enter', purpose='Read missing detail')
        self.assertTrue(pressed['ok'], pressed)
        self.assertEqual(pressed['url'], self.base + '/one')
        blocked = self.action(process, op='click', selector='#nav', purpose='Unobserved target')
        self.assertFalse(blocked['ok'], blocked)
        self.assertEqual(len(json.loads(self.statefile(url).read_text())['internal_pages']), 1)
        self.close(process)

    def test_failed_get_does_not_forbid_browser_or_reset_clock(self):
        FixtureHandler.slow_calls = 0
        url = self.base + '/slow'
        before = time.monotonic()
        get = subprocess.run(self.command(url, '--get64'), env=self.env, cwd=self.root,
            capture_output=True, text=True, timeout=8)
        self.assertNotEqual(get.returncode, 0)
        self.assertLess(time.monotonic() - before, 6)
        checkpoint = json.loads(self.statefile(url).read_text())
        self.assertFalse(json.loads(get.stdout)['ok'])
        process = self.start(url)
        reply = self.action(process, op='goto', url=url)
        self.assertTrue(reply['ok'], reply)
        self.assertEqual(reply['budget']['started_at'], checkpoint['started_at'])
        self.assertGreater(reply['budget']['elapsed_ms'], 4900)
        self.close(process)

    def test_preliminary_get_reads_first_redirect_without_contacting_chain(self):
        for status in [301, 302]:
            with self.subTest(status=status):
                endpoint = f'/get-{status}/0'
                url = self.base + endpoint
                FixtureHandler.redirect_requests.clear()
                result = subprocess.run(self.command(url, '--get64'), env=self.env,
                    cwd=self.root, capture_output=True, text=True, timeout=8)
                self.assertEqual(result.returncode, 0, result.stderr)
                reply = json.loads(result.stdout)
                self.assertTrue(reply['ok'], reply)
                self.assertTrue(reply['tls_verification'])
                self.assertEqual(reply['http_status'], status)
                self.assertEqual(reply['url'], url)
                self.assertEqual(FixtureHandler.redirect_requests, [endpoint])
                self.assertEqual(reply['budget']['internal_pages'], [])
                self.assertFalse(reply['budget']['initial_navigation_started'])
                self.assertEqual(reply['budget']['reloads'], 0)
                self.assertEqual(json.loads(self.statefile(url).read_text())['started_at'], reply['budget']['started_at'])
                self.assertFalse(list((self.home / 'url-visits').glob('*.lock')))

    def test_successful_get_does_not_reset_browser_budget(self):
        url = self.base + '/index'
        get = subprocess.run(self.command(url, '--get64'), env=self.env, cwd=self.root,
            capture_output=True, text=True, timeout=8)
        self.assertEqual(get.returncode, 0, get.stderr)
        response = json.loads(get.stdout)
        self.assertEqual(response['http_status'], 200)
        checkpoint = response['budget']
        process = self.start(url)
        reply = self.action(process, op='goto', url=url)
        self.assertTrue(reply['ok'], reply)
        self.assertEqual(reply['budget']['started_at'], checkpoint['started_at'])
        self.assertGreaterEqual(reply['budget']['elapsed_ms'], checkpoint['elapsed_ms'])
        self.close(process)

    def test_resume_same_url_lease_and_stdin_eof_cleanup(self):
        url = self.base + '/index'
        process = self.start(url)
        started = json.loads(self.statefile(url).read_text())['started_at']
        self.assertTrue(self.action(process, op='goto', url=url)['ok'])
        concurrent = subprocess.run(self.command(url), env=self.env, cwd=self.root,
            input='', capture_output=True, text=True, timeout=8)
        self.assertNotEqual(concurrent.returncode, 0)
        self.assertIn('URL already in use', concurrent.stderr)
        process.stdin.close()
        self.assertEqual(process.wait(timeout=8), 0, process.stderr.read())
        self.assert_child_gone(process)
        self.assertTrue(json.loads(self.statefile(url).read_text())['browser_closed'])
        resumed = self.start(url)
        first = self.action(resumed, op='goto', url=url)
        self.assertTrue(first['ok'], first)
        self.assertEqual(first['budget']['started_at'], started)
        self.assertFalse(self.action(resumed, op='goto', url=url)['ok'])
        self.close(resumed)

    def test_server_send_close_has_actual_owned_exit_and_removed_socket(self):
        url = self.base + '/index'
        process = self.start(url, '--server')
        def send(command):
            result = subprocess.run([NODE, self.lib / 'sbs-chromium.mjs', '--send', process.session, json.dumps(command)],
                env=self.env, cwd=self.root, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertTrue(send({'op': 'goto', 'url': url})['ok'])
        self.assertTrue(send({'op': 'close'})['ok'])
        self.assertEqual(process.wait(timeout=8), 0, process.stderr.read())
        self.assert_child_gone(process)
        self.assertTrue(json.loads(self.statefile(url).read_text())['browser_closed'])
        self.assertFalse((Path(tempfile.gettempdir()) / process.session).exists())

    def test_actual_captcha_grant_and_five_choices(self):
        url = self.base + '/index'
        policy = {'captcha_grant': {'authorized': True, 'grant_id': 'fixture',
            'scope': 'url', 'statement': 'local fixture only', 'urls': [url]}}
        (self.home / 'sbs-task-policy.json').write_text(json.dumps(policy))
        process = self.start(url)
        original = self.action(process, op='goto', url=url)
        false = self.action(process, op='captcha_begin', evidence='claimed challenge')
        self.assertFalse(false['ok'], false)
        self.assertFalse(json.loads(self.statefile(url).read_text())['captcha_encountered'])
        observed = self.action(process, op='goto', url=self.base + '/captcha', purpose='Necessary visible challenge')
        begin = self.action(process, op='captcha_begin', evidence='Visible hcaptcha fixture', observation_id=observed['observation_id'])
        self.assertTrue(begin['ok'], begin)
        self.assertTrue(begin['budget']['captcha_encountered'])
        for _ in range(5):
            self.assertTrue(self.action(process, op='click', selector='#choice')['ok'])
        self.assertFalse(self.action(process, op='click', selector='#choice')['ok'])
        self.assertTrue(self.action(process, op='captcha_end')['ok'])
        self.close(process)

    def test_custom_captcha_requires_fresh_observation_and_explicit_report(self):
        url = self.base + '/custom-captcha'
        (self.home / 'sbs-task-policy.json').write_text(json.dumps({'captcha_grant': {
            'authorized': True, 'grant_id': 'fixture', 'scope': 'url',
            'statement': 'local custom color/image fixture only', 'urls': [url]}}))
        process = self.start(url)
        old = self.action(process, op='goto', url=url)
        fresh = self.action(process, op='observe')
        stale = self.action(process, op='captcha_begin', observation_id=old['observation_id'],
            evidence='Custom color challenge explicitly observed', challenge_observed=True)
        self.assertFalse(stale['ok'], stale)
        missing = self.action(process, op='captcha_begin', observation_id=fresh['observation_id'],
            evidence='Custom color challenge explicitly observed')
        self.assertFalse(missing['ok'], missing)
        begin = self.action(process, op='captcha_begin', observation_id=fresh['observation_id'],
            evidence='Custom color challenge explicitly observed', challenge_observed=True)
        self.assertTrue(begin['ok'], begin)
        self.assertFalse(begin['challenge_markers_detected'])
        self.action(process, op='captcha_end')
        self.close(process)

    def test_work_deadline_reserves_cleanup_and_kills_only_owned_browser(self):
        url = self.base + '/index'
        folder = self.home / 'url-visits'
        folder.mkdir(mode=0o700)
        now = int(time.time() * 1000)
        state = {'version': 1, 'url': url, 'started_at': now - 171000, 'saved_at': now,
            'elapsed_ms': 171000, 'captcha_encountered': False, 'captcha_elapsed_ms': 0,
            'captcha_active': False, 'captcha_choices': 0, 'internal_pages': [],
            'observed_primary_url': url, 'reloads': 0, 'initial_navigation_started': False,
            'browser_closed': True}
        self.statefile(url).write_text(json.dumps(state))
        self.statefile(url).chmod(0o600)
        before = time.monotonic()
        process = self.start(url)
        self.assertEqual(process.wait(timeout=10), 124, process.stderr.read())
        self.assertLess(time.monotonic() - before, 9)
        self.assert_child_gone(process)
        self.assertTrue(json.loads(self.statefile(url).read_text())['browser_closed'])
        self.assertFalse(list(folder.glob('*.lock')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
