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
from agent_lab.scorer import same_origin  # noqa: E402

TEST_CONFIG = Path(__file__).resolve().parent / "config.test.toml"

GOOD_CLOCK = """<!doctype html><html><head><meta charset="utf-8"><title>UTC Clock</title></head><body>
<h1>UTC Clock</h1><div id="clock">--:--:--</div><button id="freeze">Freeze</button>
<script>
let frozen=false;
function tick(){if(frozen)return;document.getElementById('clock').textContent=new Date().toISOString().slice(11,19);}
tick();setInterval(tick,1000);
document.getElementById('freeze').onclick=()=>{frozen=!frozen;};
</script></body></html>
"""

# Looks right, is not: the time never changes and freeze does nothing.
BROKEN_CLOCK = """<!doctype html><html><head><meta charset="utf-8"><title>UTC Clock</title></head><body>
<h1>UTC Clock</h1><div id="clock">12:00:00</div><button id="freeze">Freeze</button>
<script>console.error('boom');</script></body></html>
"""


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
        with serving(GOOD_CLOCK) as url:
            report = scorer.score(url, "easy", self.config)
        self.assertEqual(report.built, scorer.PASS, report.built_detail)
        self.assertEqual(report.shipped, scorer.PASS, report.shipped_detail)
        self.assertEqual(report.http_status, 200)
        self.assertTrue(all(s == scorer.PASS for s in self.statuses(report).values()))


class BrokenBuild(ScorerCase):
    def test_broken_page_fails_the_checks_it_should(self):
        with serving(BROKEN_CLOCK) as url:
            report = scorer.score(url, "easy", self.config)
        statuses = self.statuses(report)
        self.assertEqual(report.built, scorer.FAIL)
        # It still ships: it returns 200 and renders. The two are independent,
        # which is the whole point of reporting them separately.
        self.assertEqual(report.shipped, scorer.PASS, report.shipped_detail)
        self.assertEqual(statuses["clock-ticks"], scorer.FAIL)
        self.assertEqual(statuses["no-console-errors"], scorer.FAIL)
        # ...and still passes the ones it genuinely meets.
        self.assertEqual(statuses["heading"], scorer.PASS)
        self.assertEqual(statuses["clock-element"], scorer.PASS)


class NothingToScore(ScorerCase):
    def test_no_url_is_not_a_pass(self):
        report = scorer.score(None, "easy", self.config)
        self.assertEqual(report.shipped, scorer.FAIL)
        self.assertNotEqual(report.built, scorer.PASS)
        self.assertEqual(len(report.manual_prompts), 8)

    def test_dead_url_does_not_ship(self):
        with serving(GOOD_CLOCK) as url:
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
        with serving(GOOD_CLOCK) as url:
            report = scorer.score(url, "easy", self.config)
        self.assertEqual(report.shipped, scorer.PASS)
        self.assertIn("127.0.0.1", report.metrics.get("final_url", ""))


class WithoutPlaywright(ScorerCase):
    def test_unscored_rather_than_guessed(self):
        """No browser means UNSCORED and manual prompts, never a guessed PASS."""
        original = scorer.playwright_available
        scorer.playwright_available = lambda: (False, "playwright is not installed (test)")
        try:
            with serving(GOOD_CLOCK) as url:
                report = scorer.score(url, "easy", self.config)
        finally:
            scorer.playwright_available = original

        self.assertEqual(report.built, scorer.UNSCORED)
        self.assertEqual(report.shipped, scorer.UNSCORED)
        self.assertEqual(report.http_status, 200)
        self.assertEqual(report.checks, [])
        self.assertEqual(len(report.manual_prompts), 8)
        self.assertTrue(all(p.startswith("[MANUAL]") for p in report.manual_prompts))


if __name__ == "__main__":
    unittest.main(verbosity=2)
