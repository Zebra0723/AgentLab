"""The scorer: hidden tests and a liveness check, run after the referee is done.

Two results, produced independently and never merged:

  BUILT    - do the hidden tests pass? (benchmarks/hidden/<level>.json)
  SHIPPED  - does the URL return 200 and render?

A site can be BUILT and not SHIPPED, or SHIPPED and not BUILT. Collapsing them
into one number is exactly the thing this harness exists to avoid.

Hidden tests are declarative. Each check has a short name, an assertion type,
and optional `steps` performed first. If Playwright is unavailable the checks
are not guessed: they are emitted as manual prompts and the run is UNSCORED.

The scorer never reads the agent's transcript, and the agent never sees this
file or benchmarks/hidden/.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import REPO_ROOT, Config
from .sandbox import RUNS_ROOT

PASS, FAIL, UNSCORED = "PASS", "FAIL", "UNSCORED"

# Chromium reports a failed resource with a message that names no URL, so a
# missing favicon is indistinguishable from a missing stylesheet by text alone.
# Correlate with the response instead, and ignore only the browser's own
# speculative requests.
IGNORED_URL = re.compile(r"/(favicon\.ico|apple-touch-icon[^/]*\.png)$", re.I)
GENERIC_RESOURCE_ERROR = re.compile(r"Failed to load resource", re.I)


@dataclass
class PageErrors:
    """What went wrong on a page, by source rather than by message text."""

    js: list[str] = field(default_factory=list)          # uncaught exceptions
    console: list[str] = field(default_factory=list)     # console.error calls
    responses: list[tuple[str, int]] = field(default_factory=list)  # 4xx/5xx

    def real(self) -> list[str]:
        """Errors that say something about the build.

        Uncaught exceptions always count. A failed request counts unless it is
        the browser asking for an icon nobody promised. The generic
        "Failed to load resource" console line is dropped because the response
        it refers to is already counted.
        """
        out = list(self.js)
        out += [f"HTTP {status} for {url}" for url, status in self.responses if not IGNORED_URL.search(url)]
        out += [text for text in self.console if not GENERIC_RESOURCE_ERROR.search(text)]
        return out

HIDDEN_DIR = REPO_ROOT / "benchmarks" / "hidden"

# Emoji clusters, including ZWJ sequences and variation selectors, so a family
# emoji counts once rather than four times.
_EMOJI_CORE = "\U0001F300-\U0001FAFF\U0001F1E6-\U0001F1FF☀-➿⬀-⯿️←-⇿"
EMOJI_RE = re.compile(
    f"[{_EMOJI_CORE}]️?(?:‍[{_EMOJI_CORE}]️?)*"
)


@dataclass
class CheckResult:
    name: str
    type: str
    status: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type, "status": self.status, "detail": self.detail}


@dataclass
class ScoreReport:
    """BUILT and SHIPPED live side by side here and are never combined."""

    level: str
    url: str | None = None
    built: str = UNSCORED
    built_detail: str = ""
    checks: list[CheckResult] = field(default_factory=list)
    shipped: str = UNSCORED
    shipped_detail: str = ""
    http_status: int | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    manual_prompts: list[str] = field(default_factory=list)
    scorer_error: str | None = None

    @property
    def passed_checks(self) -> int:
        return sum(1 for c in self.checks if c.status == PASS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "url": self.url,
            "built": self.built,
            "built_detail": self.built_detail,
            "checks": [c.to_dict() for c in self.checks],
            "shipped": self.shipped,
            "shipped_detail": self.shipped_detail,
            "http_status": self.http_status,
            "metrics": self.metrics,
            "manual_prompts": self.manual_prompts,
            "scorer_error": self.scorer_error,
        }


# -- Playwright plumbing ---------------------------------------------------


def playwright_available() -> tuple[bool, str]:
    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError as exc:
        return False, f"playwright is not installed ({exc})"
    return True, ""


def chromium_executable() -> str | None:
    """Find a Chromium the installed Playwright can drive.

    Preinstalled browser bundles often do not match the version the Python
    package expects, so fall back to whatever chromium build is on disk.
    """
    explicit = os.environ.get("AGENT_LAB_CHROMIUM")
    if explicit:
        return explicit
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "") or "")
    if not root.is_dir():
        return None
    candidates = sorted(root.glob("chromium*/chrome-linux/chrome"), reverse=True)
    return str(candidates[0]) if candidates else None


class _Browser:
    """Launches chromium, retrying with an explicit binary if the default fails."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._pw = None
        self._browser = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch()
        except Exception:
            path = chromium_executable()
            if not path:
                self._pw.stop()
                raise
            self._browser = self._pw.chromium.launch(executable_path=path)
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()

    def new_page(self):
        context = self._browser.new_context(
            viewport={"width": self.config.scorer.viewport_width, "height": self.config.scorer.viewport_height}
        )
        page = context.new_page()
        errors = PageErrors()
        page.on("console", lambda m: errors.console.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.js.append(str(e)))
        page.on("response", lambda r: errors.responses.append((r.url, r.status)) if r.status >= 400 else None)
        return page, context, errors


# -- check execution -------------------------------------------------------


def _set_value(page, selector: str, value: str, timeout: int) -> None:
    """Set a control's value, whatever kind of control it turned out to be."""
    locator = page.locator(selector).first
    locator.wait_for(state="attached", timeout=timeout)
    try:
        locator.fill(value, timeout=timeout)
        return
    except Exception:
        pass
    try:
        locator.select_option(value, timeout=timeout)
        return
    except Exception:
        pass
    # Range inputs, custom widgets: set it directly and announce the change.
    page.evaluate(
        """([sel, val]) => {
            const el = document.querySelector(sel);
            if (!el) return;
            const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value');
            if (setter && setter.set) { setter.set.call(el, val); } else { el.value = val; }
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        }""",
        [selector, value],
    )


def _run_steps(page, steps: list[dict[str, Any]], timeout: int) -> None:
    for step in steps or []:
        if "click" in step:
            page.locator(step["click"]).first.click(timeout=timeout)
        elif "fill" in step:
            for selector, value in step["fill"].items():
                _set_value(page, selector, str(value), timeout)
        elif "press" in step:
            page.locator(step["press"]["selector"]).first.press(step["press"]["key"], timeout=timeout)
        elif "reload" in step:
            page.reload(wait_until="load")
        elif "wait_ms" in step:
            page.wait_for_timeout(int(step["wait_ms"]))
        else:
            raise ValueError(f"unknown step {step!r}")


def _text_of(page, selector: str, timeout: int) -> str:
    locator = page.locator(selector).first
    locator.wait_for(state="attached", timeout=timeout)
    return (locator.inner_text() or "").strip()


def _assert(page, check: dict[str, Any], errors: "PageErrors", timeout: int) -> tuple[bool, str]:
    kind = check["type"]

    if kind == "exists":
        page.locator(check["selector"]).first.wait_for(state="attached", timeout=timeout)
        return True, f"{check['selector']} present"

    if kind == "absent":
        return page.locator(check["selector"]).count() == 0, f"{check['selector']} absent"

    if kind == "all_exist":
        missing = []
        for selector in check["selectors"]:
            try:
                page.locator(selector).first.wait_for(state="attached", timeout=timeout)
            except Exception:
                missing.append(selector)
        return not missing, ("all present" if not missing else f"missing {', '.join(missing)}")

    if kind == "count":
        actual = page.locator(check["selector"]).count()
        return actual == int(check["expect"]), f"{check['selector']} count={actual} expected={check['expect']}"

    if kind == "text_present":
        body = page.inner_text("body")
        return check["text"] in body, f"looked for {check['text']!r}"

    if kind == "text_absent":
        body = page.inner_text("body")
        return check["text"] not in body, f"looked for {check['text']!r}"

    if kind == "text_matches":
        text = _text_of(page, check["selector"], timeout)
        return bool(re.search(check["pattern"], text)), f"{check['selector']} = {text[:60]!r}"

    if kind == "text_equals":
        text = _text_of(page, check["selector"], timeout)
        return text == check["text"], f"{check['selector']} = {text[:60]!r}"

    if kind == "value_matches":
        locator = page.locator(check["selector"]).first
        locator.wait_for(state="attached", timeout=timeout)
        value = locator.input_value()
        return bool(re.search(check["pattern"], value or "")), f"{check['selector']} value = {value!r}"

    if kind in ("text_changes", "text_stable"):
        before = _text_of(page, check["selector"], timeout)
        page.wait_for_timeout(int(check.get("wait_ms", 1500)))
        after = _text_of(page, check["selector"], timeout)
        changed = before != after
        want_change = kind == "text_changes"
        detail = f"{before[:30]!r} -> {after[:30]!r}"
        return (changed == want_change), detail

    if kind == "no_console_errors":
        # Let late-arriving resource errors land, so this check does not depend
        # on how fast the page happened to settle.
        page.wait_for_timeout(int(check.get("settle_ms", 800)))
        found = errors.real()
        return not found, (f"{len(found)} console error(s): {found[0][:80]}" if found else "clean console")

    raise ValueError(f"unknown check type {kind!r}")


def run_checks(url: str, checks: list[dict[str, Any]], config: Config) -> list[CheckResult]:
    """Run every hidden check in its own fresh browser context."""
    timeout = config.scorer.check_timeout_ms
    results: list[CheckResult] = []
    with _Browser(config) as browser:
        for check in checks:
            page, context, errors = browser.new_page()
            try:
                page.goto(url, wait_until="load", timeout=timeout)
                _run_steps(page, check.get("steps", []), timeout)
                ok, detail = _assert(page, check, errors, timeout)
                results.append(CheckResult(check["name"], check["type"], PASS if ok else FAIL, detail))
            except Exception as exc:
                results.append(CheckResult(check["name"], check["type"], FAIL, _short(exc)))
            finally:
                context.close()
    return results


# -- SHIPPED and mechanical metrics ---------------------------------------


def _opener(url: str):
    """A local URL must not be sent through an outbound proxy."""
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def same_origin(first: str, second: str) -> bool:
    """Same host, ignoring scheme, port and a leading www."""
    def host(u: str) -> str:
        h = (urllib.parse.urlsplit(u).hostname or "").lower()
        return h[4:] if h.startswith("www.") else h

    return host(first) == host(second)


def http_status(url: str, timeout: float) -> tuple[int | None, str, str]:
    """Status, a note, and the URL the request actually ended up at.

    urllib follows redirects, so without the final URL a deployment that hands
    every visitor to a login wall on another host looks like a healthy 200.
    """
    request = urllib.request.Request(url, headers={"User-Agent": "agent-lab-scorer"})
    try:
        with _opener(url).open(request, timeout=timeout) as response:
            return response.status, "", response.url or url
    except urllib.error.HTTPError as exc:
        return exc.code, f"HTTP {exc.code}", getattr(exc, "url", url) or url
    except Exception as exc:
        return None, _short(exc), url


def check_shipped(url: str, config: Config) -> tuple[str, str, int | None, dict[str, Any]]:
    """200 and renders, at the URL that was deployed. Or it is not SHIPPED.

    "Returns 200" is not the same claim as "serves the app". A deployment
    behind Vercel's Deployment Protection answers every anonymous visitor with
    a 302 to vercel.com/sso-api, which renders a perfectly healthy login page.
    Following that quietly and calling it SHIPPED reports the platform's
    availability, not the agent's. An off-origin redirect fails, and the log
    names where it went.
    """
    status, note, final_url = http_status(url, config.scorer.http_timeout_seconds)
    metrics: dict[str, Any] = {"final_url": final_url}
    if status != 200:
        return FAIL, note or f"HTTP {status}", status, metrics

    if not same_origin(url, final_url):
        host = urllib.parse.urlsplit(final_url).hostname or final_url
        metrics["redirected_off_origin"] = True
        return (
            FAIL,
            f"redirected off-origin to {host} - the deployment is not serving the app to the public",
            status,
            metrics,
        )

    available, reason = playwright_available()
    if not available:
        return UNSCORED, f"HTTP 200, but rendering not checked: {reason}", status, metrics

    try:
        with _Browser(config) as browser:
            page, context, errors = browser.new_page()
            try:
                page.goto(url, wait_until="load", timeout=config.scorer.check_timeout_ms)
                page.wait_for_timeout(500)
                body = page.inner_text("body") or ""
                elements = page.evaluate("() => document.body ? document.body.querySelectorAll('*').length : 0")
                emoji = EMOJI_RE.findall(body)
                metrics.update({
                    "emoji_count": len(emoji),
                    "emoji_distinct": len(set(emoji)),
                    "rendered_text_chars": len(body.strip()),
                    "dom_elements": int(elements),
                    "console_errors": len(errors.real()),
                    "title": (page.title() or "")[:120],
                })
                rendered = bool(body.strip()) and int(elements) > 0
                detail = f"HTTP 200, {int(elements)} elements, {len(body.strip())} chars of text"
                return (PASS if rendered else FAIL), (detail if rendered else "HTTP 200 but the page renders nothing"), status, metrics
            finally:
                context.close()
    except Exception as exc:
        return UNSCORED, f"HTTP 200, but the browser could not be driven: {_short(exc)}", status, metrics


# -- manual fallback -------------------------------------------------------


def manual_prompt(check: dict[str, Any]) -> str:
    kind, name = check["type"], check["name"]
    steps = check.get("steps") or []
    prefix = ""
    if steps:
        described = []
        for step in steps:
            if "click" in step:
                described.append(f"click {step['click']}")
            elif "fill" in step:
                described.append("set " + ", ".join(f"{k}={v!r}" for k, v in step["fill"].items()))
            elif "reload" in step:
                described.append("reload the page")
            elif "wait_ms" in step:
                described.append(f"wait {step['wait_ms']}ms")
            elif "press" in step:
                described.append(f"press {step['press']['key']} in {step['press']['selector']}")
        prefix = "; ".join(described) + " -> "

    body = {
        "exists": lambda: f"is {check.get('selector')} on the page?",
        "absent": lambda: f"is {check.get('selector')} absent?",
        "all_exist": lambda: f"are all of {', '.join(check.get('selectors', []))} present?",
        "count": lambda: f"are there exactly {check.get('expect')} of {check.get('selector')}?",
        "text_present": lambda: f"does the page show {check.get('text')!r}?",
        "text_absent": lambda: f"is {check.get('text')!r} nowhere on the page?",
        "text_matches": lambda: f"does {check.get('selector')} match /{check.get('pattern')}/?",
        "text_equals": lambda: f"does {check.get('selector')} read exactly {check.get('text')!r}?",
        "value_matches": lambda: f"does {check.get('selector')}'s value match /{check.get('pattern')}/?",
        "text_changes": lambda: f"does {check.get('selector')} change within {check.get('wait_ms', 1500)}ms?",
        "text_stable": lambda: f"does {check.get('selector')} stay unchanged for {check.get('wait_ms', 1500)}ms?",
        "no_console_errors": lambda: "is the browser console free of errors?",
    }.get(kind, lambda: f"check {kind}")()
    return f"[MANUAL] {name}: {prefix}{body}"


# -- top level -------------------------------------------------------------


def load_checks(level: str, path: Path | None = None) -> list[dict[str, Any]]:
    source = path or (HIDDEN_DIR / f"{level}.json")
    data = json.loads(source.read_text(encoding="utf-8"))
    return list(data["checks"])


def score(url: str | None, level: str, config: Config, checks_path: Path | None = None) -> ScoreReport:
    report = ScoreReport(level=level, url=url)
    try:
        checks = load_checks(level, checks_path)
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        report.scorer_error = f"could not load hidden tests for {level}: {_short(exc)}"
        report.built_detail = report.scorer_error
        report.shipped_detail = report.scorer_error
        return report

    if not url:
        report.built_detail = "no URL: nothing was deployed, so the hidden tests could not run"
        report.shipped = FAIL
        report.shipped_detail = "no URL was produced by the run"
        report.manual_prompts = [manual_prompt(c) for c in checks]
        return report

    # SHIPPED first: it is independent of BUILT and cheap.
    report.shipped, report.shipped_detail, report.http_status, report.metrics = check_shipped(url, config)

    available, reason = playwright_available()
    if not available:
        report.built = UNSCORED
        report.built_detail = f"{reason}; hidden tests were not run and are listed below as manual checks"
        report.manual_prompts = [manual_prompt(c) for c in checks]
        return report

    try:
        report.checks = run_checks(url, checks, config)
    except Exception as exc:
        report.built = UNSCORED
        report.built_detail = f"the browser could not be driven: {_short(exc)}"
        report.manual_prompts = [manual_prompt(c) for c in checks]
        return report

    failed = [c for c in report.checks if c.status != PASS]
    report.built = PASS if not failed else FAIL
    report.built_detail = f"{report.passed_checks}/{len(report.checks)} hidden tests passed"
    return report


def _short(exc: Any, width: int = 140) -> str:
    text = re.sub(r"\s+", " ", str(exc)).strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def _resolve_run(run: str) -> Path:
    candidate = Path(run)
    return candidate if candidate.is_dir() else RUNS_ROOT / run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent_lab.scorer", description="Score a deployed URL: BUILT (hidden tests) and SHIPPED (200 + renders)."
    )
    parser.add_argument("--url", help="the deployed URL to score")
    parser.add_argument("--level", help="benchmark level (easy/medium/hard)")
    parser.add_argument("--run", help="run id or runs/<id> path; reads url and level from run.json and writes score.json")
    parser.add_argument("--checks", type=Path, help="override the hidden test file")
    parser.add_argument("--out", type=Path, help="write the report JSON here")
    parser.add_argument("--config", type=Path, help="path to config.toml")
    args = parser.parse_args(argv)

    config = Config.load(args.config)
    url, level, run_dir = args.url, args.level, None

    if args.run:
        run_dir = _resolve_run(args.run)
        record_path = run_dir / "run.json"
        if not record_path.exists():
            print(f"no run.json in {run_dir}", file=sys.stderr)
            return 1
        record = json.loads(record_path.read_text(encoding="utf-8"))
        level = level or record.get("level")
        url = url or record.get("deploy_url")

    if not level:
        print("--level is required (or use --run)", file=sys.stderr)
        return 1

    report = score(url, level, config, args.checks)

    out = args.out or (run_dir / "score.json" if run_dir else None)
    if out:
        out.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")

    print(f"BUILT:   {report.built}  ({report.built_detail})")
    for check in report.checks:
        print(f"  {check.status:<8} {check.name:<24} {check.detail}")
    for prompt in report.manual_prompts:
        print(f"  {prompt}")
    print(f"SHIPPED: {report.shipped}  ({report.shipped_detail})")
    if report.metrics:
        print(f"metrics: {json.dumps(report.metrics)}")
    if out:
        print(f"wrote {out}")
    return 3 if UNSCORED in (report.built, report.shipped) else 0


if __name__ == "__main__":
    raise SystemExit(main())
