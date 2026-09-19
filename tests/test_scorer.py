"""Proof that the scorer passes what it should and fails what it should.

A referee that only ever says PASS is no referee, so these tests serve a
correct page, a broken page and no page at all, and check each verdict. The
pages are served from a local http.server on a throwaway port.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import functools
import http.server
import socketserver
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_lab import scorer  # noqa: E402
from agent_lab.config import Config  # noqa: E402
from agent_lab.scorer import run_checks, same_origin  # noqa: E402

TEST_CONFIG = Path(__file__).resolve().parent / "config.test.toml"

GOOD_TODO = """<!doctype html><html><head><meta charset="utf-8"><title>To-do</title></head><body>
<h1>To-do</h1>
<input id="new-todo"><button id="add-todo">Add</button>
<ul id="todo-list"></ul>
<script>
const KEY = 'todos';
let todos = JSON.parse(localStorage.getItem(KEY) || '[]');
function save() { localStorage.setItem(KEY, JSON.stringify(todos)); }
function render() {
  const ul = document.getElementById('todo-list'); ul.innerHTML = '';
  todos.forEach((t, i) => {
    const li = document.createElement('li');
    const s = document.createElement('span'); s.textContent = t; li.appendChild(s);
    const b = document.createElement('button'); b.className = 'delete'; b.textContent = 'x';
    b.onclick = () => { todos.splice(i, 1); save(); render(); };
    li.appendChild(b); ul.appendChild(li);
  });
}
document.getElementById('add-todo').onclick = () => {
  const inp = document.getElementById('new-todo');
  const v = (inp.value || '').trim();
  if (!v) return;
  todos.push(v); save(); inp.value = ''; render();
};
render();
</script></body></html>
"""

# Looks right, is not: nothing is persisted, so a reload loses the list.
BROKEN_TODO = GOOD_TODO.replace("localStorage.setItem(KEY, JSON.stringify(todos));", "/* never saved */") \
                       .replace("JSON.parse(localStorage.getItem(KEY) || '[]')", "[]")


@contextlib.contextmanager
def serving(html: str):
    """Serve one page on an ephemeral port for the life of the block."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "index.html").write_text(html, encoding="utf-8")
        handler = functools.partial(QuietHandler, directory=tmp)
        with socketserver.TCPServer(("127.0.0.1", 0), handler) as httpd:
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                yield f"http://127.0.0.1:{httpd.server_address[1]}/"
            finally:
                httpd.shutdown()
                thread.join(timeout=5)


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep the test output readable
        pass


@contextlib.contextmanager
def redirecting_to(target_host: str):
    """Serve a 302 to another host, the way a protected deployment does."""

    class Redirector(QuietHandler):
        def do_GET(self):
            # The destination serves a real page, as a login wall does. Only
            # the entry point redirects, so there is no loop.
            if self.path.startswith("/wall"):
                body = b"<!doctype html><html><body><h1>Sign in to continue</h1></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(302)
            self.send_header("Location", f"http://{target_host}:{self.server.server_address[1]}/wall")
            self.end_headers()

    with socketserver.TCPServer(("127.0.0.1", 0), Redirector) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{httpd.server_address[1]}/"
        finally:
            httpd.shutdown()
            thread.join(timeout=5)


class OriginCheck(unittest.TestCase):
    """A deployment that hands visitors to someone else has not shipped.

    Regression: a live run deployed behind Vercel's Deployment Protection. The
    URL answered 302 -> vercel.com/sso-api, urllib and Playwright both followed
    it, and the scorer reported SHIPPED: PASS for Vercel's login page - 145 DOM
    elements of somebody else's app.
    """

    def test_ordinary_redirects_stay_same_origin(self):
        self.assertTrue(same_origin("https://x.vercel.app/", "https://x.vercel.app/index.html"))
        self.assertTrue(same_origin("http://x.vercel.app", "https://x.vercel.app/"))
        self.assertTrue(same_origin("https://x.vercel.app", "https://www.x.vercel.app/"))

    def test_a_different_host_is_not_the_same_origin(self):
        self.assertFalse(same_origin("https://x.vercel.app/", "https://vercel.com/sso-api?url=x"))
        self.assertFalse(same_origin("https://x.vercel.app/", "https://evil.example/"))


class ScorerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = Config.load(TEST_CONFIG)
        available, reason = scorer.playwright_available()
        if not available:
            raise unittest.SkipTest(f"playwright unavailable: {reason}")

    def statuses(self, report) -> dict[str, str]:
        return {c.name: c.status for c in report.checks}


class GoodBuild(ScorerCase):
    def test_correct_page_is_built_and_shipped(self):
        with serving(GOOD_TODO) as url:
            report = scorer.score(url, "easy", self.config)
        self.assertEqual(report.built, scorer.PASS, report.built_detail)
        self.assertEqual(report.shipped, scorer.PASS, report.shipped_detail)
        self.assertEqual(report.http_status, 200)
        self.assertTrue(all(s == scorer.PASS for s in self.statuses(report).values()))


class BrokenBuild(ScorerCase):
    def test_broken_page_fails_the_checks_it_should(self):
        with serving(BROKEN_TODO) as url:
            report = scorer.score(url, "easy", self.config)
        statuses = self.statuses(report)
        self.assertEqual(report.built, scorer.FAIL)
        # It still ships: it returns 200 and renders. The two are independent,
        # which is the whole point of reporting them separately.
        self.assertEqual(report.shipped, scorer.PASS, report.shipped_detail)
        self.assertEqual(statuses["task-survives-reload"], scorer.FAIL)
        # ...and still passes the ones it genuinely meets.
        self.assertEqual(statuses["add-task-appears"], scorer.PASS)
        self.assertEqual(statuses["delete-removes-task"], scorer.PASS)


class NothingToScore(ScorerCase):
    def test_no_url_is_not_a_pass(self):
        report = scorer.score(None, "easy", self.config)
        self.assertEqual(report.shipped, scorer.FAIL)
        self.assertNotEqual(report.built, scorer.PASS)
        self.assertEqual(len(report.manual_prompts), 4)

    def test_dead_url_does_not_ship(self):
        with serving(GOOD_TODO) as url:
            dead = url + "missing-page.html"
            report = scorer.score(dead, "easy", self.config)
        self.assertEqual(report.shipped, scorer.FAIL)
        self.assertEqual(report.http_status, 404)


class OffOriginRedirect(ScorerCase):
    def test_a_redirect_to_another_host_fails_shipped(self):
        with redirecting_to("localhost") as url:
            report = scorer.score(url, "easy", self.config)
        self.assertEqual(report.shipped, scorer.FAIL)
        self.assertIn("off-origin", report.shipped_detail)
        self.assertEqual(report.http_status, 200)
        self.assertTrue(report.metrics.get("redirected_off_origin"))
        self.assertIn("localhost", report.metrics.get("final_url", ""))

    def test_the_final_url_is_always_recorded(self):
        with serving(GOOD_TODO) as url:
            report = scorer.score(url, "easy", self.config)
        self.assertEqual(report.shipped, scorer.PASS)
        self.assertIn("127.0.0.1", report.metrics.get("final_url", ""))


LIVE_STATE = """<!doctype html><html><body><div id="box">live</div>
<script>
window.state = {ticks: 0, ups: 0};
setInterval(() => { window.state.ticks++; }, 50);
addEventListener('keydown', e => { if (e.key === 'ArrowUp') window.state.ups++; });
</script></body></html>
"""


class JsStateChecks(ScorerCase):
    """A game's truth is in its state object, not its pixels."""

    def statuses_for(self, checks, html=LIVE_STATE):
        with serving(html) as url:
            return {c.name: (c.status, c.detail) for c in run_checks(url, checks, self.config)}

    def test_evaluate_reads_the_pages_own_state(self):
        result = self.statuses_for([
            {"name": "has-state", "type": "evaluate", "expression": "typeof window.state === 'object'"},
            {"name": "wrong-value", "type": "evaluate", "expression": "window.state.ticks", "expect": -1},
        ])
        self.assertEqual(result["has-state"][0], scorer.PASS)
        self.assertEqual(result["wrong-value"][0], scorer.FAIL)

    def test_evaluate_changes_catches_movement_and_stillness(self):
        result = self.statuses_for([
            {"name": "moves", "type": "evaluate_changes", "expression": "window.state.ticks", "wait_ms": 300},
            {"name": "still", "type": "evaluate_changes", "expression": "window.state.ups", "wait_ms": 300},
        ])
        self.assertEqual(result["moves"][0], scorer.PASS)
        self.assertEqual(result["still"][0], scorer.FAIL, "nothing pressed a key, so this must not change")

    def test_a_held_key_is_seen_between_the_readings(self):
        """Regression: press() is a keydown and keyup in the same instant, and
        steps run before the readings, so neither could measure a held key."""
        result = self.statuses_for([{
            "name": "holds", "type": "evaluate_changes", "expression": "window.state.ups",
            "steps_between": [{"hold": {"key": "ArrowUp", "ms": 200}}],
        }])
        self.assertEqual(result["holds"][0], scorer.PASS, result["holds"][1])

    def test_wait_for_expression_waits(self):
        result = self.statuses_for([
            {"name": "arrives", "type": "wait_for_expression", "timeout_ms": 5000,
             "expression": "() => window.state.ticks > 5"},
            {"name": "never", "type": "wait_for_expression", "timeout_ms": 1200,
             "expression": "() => window.state.ups > 99"},
        ])
        self.assertEqual(result["arrives"][0], scorer.PASS)
        self.assertEqual(result["never"][0], scorer.FAIL)


class WithoutPlaywright(ScorerCase):
    def test_unscored_rather_than_guessed(self):
        """No browser means UNSCORED and manual prompts, never a guessed PASS."""
        original = scorer.playwright_available
        scorer.playwright_available = lambda: (False, "playwright is not installed (test)")
        try:
            with serving(GOOD_TODO) as url:
                report = scorer.score(url, "easy", self.config)
        finally:
            scorer.playwright_available = original

        self.assertEqual(report.built, scorer.UNSCORED)
        self.assertEqual(report.shipped, scorer.UNSCORED)
        self.assertEqual(report.http_status, 200)
        self.assertEqual(report.checks, [])
        self.assertEqual(len(report.manual_prompts), 4)
        self.assertTrue(all(p.startswith("[MANUAL]") for p in report.manual_prompts))


if __name__ == "__main__":
    unittest.main(verbosity=2)
