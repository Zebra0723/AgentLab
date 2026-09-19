# agent-lab

A harness that runs coding agents against fixed benchmarks and scores them.

The harness is the referee. An agent gets a prompt and a sandbox directory, and
nothing else: it never sees the referee's caps, the hidden tests, or the scoring
code. Everything the harness records is derived from the agent's own protocol
stream, not from anything the agent says about itself.

---

## Start a run

```bash
export GITHUB_TOKEN=...      # required: the run refuses to start without both
export VERCEL_TOKEN=...

python3 -m agent_lab.runner --level easy --arm arm1
```

That builds `runs/<id>/`, runs the arm under the referee, scores the result and
writes the morning log. `--list` shows the arms that exist:

```bash
python3 -m agent_lab.runner --list
```

Useful flags:

| flag | what it does |
| --- | --- |
| `--level easy\|medium\|hard` | which benchmark task |
| `--arm <name>` | which agent setup |
| `--url <url>` | score against this URL instead of the one the run produced |
| `--no-score` | run only; score later |
| `--config <path>` | use a different config.toml |
| `--fake-script <path>` | script for the `fake` arm (see Testing) |

Score or re-score an existing run, and rewrite its log:

```bash
python3 -m agent_lab.scorer --run <run-id> --url https://your-site.vercel.app
python3 -m agent_lab.logtool --run <run-id> --print
```

`scorer.py` also runs standalone against any URL:

```bash
python3 -m agent_lab.scorer --url https://example.vercel.app --level easy
```

If the deploy leg is unavailable - no Vercel account, no network - you can still
get a BUILT result by serving what the agent built and scoring that. SHIPPED
then means "serves locally", so say so when you report it:

```bash
(cd runs/<id>/workspace && python3 -m http.server 8000 &)
python3 -m agent_lab.scorer --run <id> --url http://127.0.0.1:8000/
python3 -m agent_lab.logtool --run <id> --print
```

## Read the log

`runs/<id>/log.txt` is one screen of plain text, no colour codes. Top to bottom:

```
========================================================================
 agent-lab   20260919-112901-easy-arm1
========================================================================
 level    easy                   arm      arm1          <- what ran
 outcome  AGENT_DONE             started  2026-09-19..  <- how it ended
 built    PASS                   shipped  PASS          <- the two results

HIDDEN TESTS   BUILT: PASS   8/8 hidden tests passed
  PASS  heading                looked for 'UTC Clock'   <- one line per check
  FAIL  clock-ticks            '12:00:01' -> '12:00:01'

SHIPPED: PASS   HTTP 200, 14 elements, 212 chars of text
  url    https://...

INSTRUCTION FOLLOWING                                   <- mechanical, every run
  asked a question     no  (0 time(s))
  emoji in shipped UI  3
  files touched        2 of 1 needed
  messages             12 of 60
  deploys              1 of 10   (1 succeeded)
  wall clock           00:04:12 of 04:00:00

LAST 5 ACTIONS                                          <- what led to the end
  [   12.1s] write     Write index.html
  [   14.8s] deploy    npx vercel --prod              <<  <- the one that ended it

DIED: agent_done at 00:04:12                            <- which condition fired
```

Three things to read first: **outcome** (why it stopped), **built** and
**shipped**. `BUILT` and `SHIPPED` are separate results and are never combined -
a site can pass every hidden test and not be reachable, or be reachable and
wrong. `UNSCORED` means the harness could not check something and refused to
guess; the hidden tests then appear as `[MANUAL]` prompts to run by hand.

Everything else in the run is on disk beside the log:

```
runs/<id>/
  log.txt             the morning log
  run.json            the full record: ledger, verdict, every action
  score.json          BUILT, SHIPPED, per-check results, metrics
  transcript.jsonl    the agent's stream, written as it happened
  workspace/          the sandbox the agent worked in (a git repo)
```

## The referee

Runs in the parent process; kills the child. Any one of these ends a run
immediately, and the one that fired is recorded with the last five actions
before it:

| condition | fires when |
| --- | --- |
| `message_cap` | assistant messages reach the cap (default 60) |
| `wall_clock` | the run reaches the time cap (4h easy and medium, 6h hard) |
| `deploy_cap` | deploy attempts reach 10 |
| `silent_loop` | the same file is written 4+ times with no deploy in between |
| `question_loop` | the agent asks the human anything twice |
| `agent_done` | the agent reports it is finished |

Caps live in `config.toml` and nowhere else. They are never rendered into the
prompt - the runner refuses to start if a cap value appears in it. The referee
has no setters: once a verdict fires, the ledger freezes and the verdict never
changes, whatever arrives afterwards.

Two definitions worth knowing before you read numbers across arms:

- A **message** is one assistant message, summed across every agent process in
  the run. One budget per run, so an arm that spends messages on coordination
  pays for them.
- A **deploy** is an *attempt*, not a success. Counting successes only would let
  a broken deploy loop run forever. `deploy_successes` is recorded separately.

On kill: SIGTERM to the process group, then SIGKILL after
`kill_grace_seconds`. An agent that ignores SIGTERM still dies (there is a test
for it).

## Arms

An arm is one agent setup under test. Every arm implements one method:

```python
run(task, workspace, referee) -> RunResult
```

To add one, drop a module into `agent_lab/arms/`:

```python
from ..agentproc import run_agent
from .base import Arm, RunResult, register

@register
class Arm2(Arm):
    name = "arm2"
    description = "planner, then builder"

    def run(self, task, workspace, referee):
        process = run_agent(task.prompt, workspace, referee, self.config, label="planner")
        ...
        return RunResult(processes=[...])
```

That is the whole integration. `runner.py`, `referee.py` and `scorer.py` never
learn its name: arms are discovered from the filesystem, and there is a test
asserting those three modules mention no arm by name. An arm is handed the
referee but gets no process handles and no caps, so it can report on its run
but cannot keep an agent alive past a verdict or score itself.

Built so far: `arm1` (single agent, one shot) and `fake` (a test double).

## Benchmarks

```
benchmarks/
  prompt.txt        the frozen agent prompt, with a {TASK} placeholder
  tasks.json        easy / medium / hard task text + expected file count
  hidden/<level>.json   the hidden tests
```

| level | build | the four checks |
| --- | --- | --- |
| easy | to-do list | add shows it, survives reload, delete removes it, empty input is harmless |
| medium | text adventure | three rooms reachable, item goes in inventory, game can be won, nonsense doesn't break it |
| hard | Pong | ball moves, paddle answers the keys, someone scores, ball never leaves the field |

Four checks per level, so a score is always out of 4. Each task names the exact
element ids and rules its checks depend on - a check cannot be deterministic if
every agent invents its own structure.

Hidden tests are declarative: a list of named checks, each with an assertion
type and optional interaction `steps`, run in a fresh browser context.

```json
{"name": "paddle-responds-to-keys", "type": "evaluate_changes",
 "expression": "window.pong.paddles.left",
 "steps_between": [{"hold": {"key": "ArrowUp", "ms": 700}}]}
```

Steps: `click`, `fill`, `press`, `hold`, `keys`, `reload`, `wait_ms`.
`steps_between` runs actions *between* the two readings of a `_changes` check -
without it, "did this input move the thing" is unmeasurable, because the move
is over before the first reading.

Assertions: `exists`, `absent`, `all_exist`, `count`, `text_present`,
`text_absent`, `text_matches`, `text_equals`, `value_matches`, `text_changes`,
`text_stable`, `no_console_errors`, `evaluate`, `evaluate_changes`,
`wait_for_expression`. The last three read the page's own live state, which is
how a game is checked: its truth is in its state object, not its pixels.

Checks run under Playwright. **If Playwright is unavailable the run is marked
UNSCORED and the checks are printed as manual prompts** - the harness never
guesses a pass.

**The hidden tests are themselves tested.** A correct and a deliberately broken
build of all three apps are scored in development; the correct one must get 4/4
and the broken one must fail exactly the check aimed at its defect. Wrong
hidden tests would poison every result in the experiment.

## Reporting

After each run the harness writes the log and, if `[email].enabled` is on,
emails it:

```bash
export SMTP_PASSWORD='your-app-specific-password'
```

Recipient, sender and SMTP host live under `[email]` in `config.toml`. The
password is only ever read from the environment. A mail problem never fails a
run; the reason is printed once and the run still stands.

For the experiment as a whole:

```bash
python3 -m agent_lab.summary
python3 -m agent_lab.summary --csv results.csv
python3 -m agent_lab.summary --email
```

That walks `runs/` and prints every run, then the four things worth comparing
across arms: ship rate, hidden tests passed, messages spent, and
instruction-following. It flags cells with fewer than three runs as one sample
rather than a measurement, and warns when runs did not all use the same model.
It deliberately does not rank arms or compute a single score.

## What the harness does not guarantee

Worth knowing before you trust a number.

- **Redaction catches credentials in recognisable form.** It masks known token
  shapes, `--token <value>` flags, and the literal value of every sensitive
  environment variable. It cannot catch a value an agent deliberately takes
  apart - `printf '%s' "$VERCEL_TOKEN" | od -c` prints it one character per
  column, and no pattern matches that. An agent that wants to exfiltrate its
  own credentials can. Treat run tokens as disposable and scoped.
- **The sandbox is where the harness puts the agent, not a jail.** The workspace
  path is verified to be inside `runs/`, the agent's working directory is set to
  it, and file tools are confined to it. Shell commands are not: `Bash` can read
  anywhere the user running the harness can read. For a stricter boundary, run
  the harness in a container.
- **The host's own configuration is part of the experiment unless you turn it
  off.** Without `bare = true` in `config.toml`, an agent process loads the
  CLAUDE.md, hooks, plugins and MCP servers it finds on the machine. Two people
  running the same arm on different laptops are not running the same experiment.
  Bare mode needs `ANTHROPIC_API_KEY`, because it does not use a subscription
  login. Record which way you ran it.
- **`model = ""` means the agent binary's default**, which changes when the
  binary changes. Pin a model before comparing arms across days.
- **Denied tool calls are counted, not prevented from mattering.** If
  `allowed_tools` is too narrow, the agent spends its budget being refused and
  scores badly for a harness reason. The log prints `tool calls REFUSED` when
  that happens; a non-zero number invalidates the comparison, not the agent.

## Testing

```bash
python3 -m unittest discover -s tests -v
```

92 tests. `tests/test_referee.py` fires every kill condition through the real
runner and a real child process - the only stand-in is the agent itself
(`agent_lab/fakeagent.py`, which replays a script and speaks the same
stream-json protocol). `tests/test_scorer.py` serves a correct page, a broken
page, a dead page and one that redirects off-origin, and checks each verdict.
`tests/test_deploy.py` covers project naming and collisions against a fake API
client and `tests/test_report.py` covers the email and the summary, so the
suite never touches the network or sends mail.

Tests use `tests/config.test.toml`: the same config shape with caps small enough
to fire in seconds.

## Requirements

Python 3.11+ (stdlib only, except Playwright for scoring), the `claude` CLI on
PATH for `arm1`, and `git`.

```bash
uv venv && uv pip install playwright && playwright install chromium
```
