# Predictions

Write the prediction down **before** the run, then don't edit it. The point of
the harness is to find out where your intuition about agent setups is wrong, and
a prediction you revise after seeing the log tells you nothing.

One row per (arm, level). Fill the prediction columns first; fill `actual` from
`runs/<id>/log.txt` afterwards. Keep the run id so the claim stays checkable.

## What to predict

The six numbers the harness records mechanically for every run:

| metric | where it comes from |
| --- | --- |
| **outcome** | which kill condition fired |
| **BUILT** | hidden tests pass / fail |
| **SHIPPED** | URL returns 200 and renders |
| **messages** | assistant messages, all agents, one budget per run |
| **deploys** | deploy attempts (successes recorded separately) |
| **asked a question** | did it stop and ask the human |
| **files touched vs needed** | distinct files written vs the level's expected count |
| **emoji in shipped UI** | emoji clusters in the rendered page text |

## arm1 - single agent, one shot

| level | predicted outcome | BUILT | SHIPPED | messages | deploys | asked? | files | emoji |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| easy | agent_done | PASS | PASS | 8-15 | 1-2 | no | 1-2 of 1 | 0-2 |
| medium | agent_done | PASS | PASS | 15-30 | 1-3 | no | 3-5 of 3 | 0-3 |
| hard | agent_done | FAIL | PASS | 30-60 | 2-5 | no | 5-9 of 6 | 0-4 |

Reasoning behind those, so they can be argued with:

- **easy** is one file with three requirements. A single agent should not need
  a second attempt at it. If this fails, suspect the harness, not the agent.
- **medium** adds arithmetic and an edge case (empty and non-numeric input).
  The NaN checks are the ones most likely to fail: they are the requirement
  easiest to read past.
- **hard** asks for a markdown renderer written by hand plus persistence plus
  search. `BUILT: FAIL` on at least one renderer check is the prediction; the
  site will still ship, because a partly-working renderer still serves 200.
  This is the case the BUILT/SHIPPED split exists for.
- **asked?** is predicted `no` throughout: the frozen prompt says nobody will
  answer. An arm that asks anyway is the interesting result.
- **files touched > needed** is the expected shape everywhere. `expected_files`
  is what a competent build needs, not a budget the agent was told about.

### Where these predictions are most likely wrong

- `messages` on easy. If the deploy step is awkward, retry traffic dominates the
  count and the estimate is low.
- `deploys` assumes the first deploy works. Every failed deploy is a counted
  attempt, so a bad token turns a 1 into a 10 and a `deploy_cap` kill.
- `emoji` is a guess. It is recorded precisely because nobody knows what agents
  do here unprompted, and the frozen prompt says nothing about tone.

## Later arms

Not built yet. Predict before building, not after.

| arm | level | predicted outcome | BUILT | SHIPPED | messages | notes |
| --- | --- | --- | --- | --- | --- | --- |
| arm2 | easy | | | | | |
| arm3 | easy | | | | | |

## Results

| run id | arm | level | outcome | BUILT | SHIPPED | messages | deploys | asked? | files | emoji |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 20260919-155642-easy-arm1-bbc7 | arm1 | easy | agent_done | PASS | PASS | 13 of 60 | 2 of 10 | no | 1 of 1 | 0 |
| 20260919-115231-easy-arm1-707b | arm1 | easy | message_cap | PASS | PASS* | 60 of 60 | 4 of 10 | no | 1 of 1 | 0 |

Row 2 is a harness shakedown, not an experiment result: it ran with a
placeholder `VERCEL_TOKEN` on the development machine, so the deploy could
never succeed. `SHIPPED` there means "serves locally". Row 1 is the real
baseline: a live agent, a real deploy to `agent-1-level-1.vercel.app`, and
hidden tests run against that URL.

### arm1 / easy: the prediction was right on every field

| field | predicted | actual |
| --- | --- | --- |
| outcome | agent_done | agent_done |
| BUILT | PASS | PASS, 8/8 |
| SHIPPED | PASS | PASS |
| messages | 8-15 | 13 |
| deploys | 1-2 | 2 |
| asked a question | no | no |
| files touched | 1-2 of 1 | 1 of 1 |
| emoji | 0-2 | 0 |

Eight for eight, in 40 seconds of wall clock. Before reading anything into
that: `easy` is one file with three requirements and a working deploy path.
Getting it right means the harness is calibrated and the caps are not binding
on a task this size - not that the prediction was insightful. A baseline that
lands exactly where you expected is the boring, necessary result; it is what
makes a later surprise legible as a surprise.

Two numbers worth carrying forward:

- **4 of 13 messages were thinking only** - 31%, against 43% on the shakedown
  run where the agent was stuck. Thinking share may turn out to track being
  stuck rather than task difficulty. Watch it on medium and hard before
  treating it as either.
- **2 deploys, both successful.** The agent deployed, checked the URL with
  curl, and deployed again. The prediction assumed one deploy plus perhaps a
  retry; what actually happened was verify-then-redeploy. Worth knowing when
  an arm's deploy count looks high: it may be diligence, not failure.

### What the shakedown run cost the prediction

Predicted `agent_done` in 8-15 messages. Got `message_cap` at 60, with 4 deploy
attempts and none successful.

The build itself was never in doubt even there: one file, 8/8 hidden tests, no
questions. Everything after the build went on the deploy. The token was a
placeholder, the Vercel CLI rejected it as malformed, and the agent spent its
remaining budget diagnosing the environment - looking for a cached credential,
reading the CLI's own source, checking proxy settings - rather than retrying
blindly. That is why it died on `message_cap` and not `deploy_cap`: a stuck
agent that thinks does not trip the deploy counter.

The "most likely wrong" note above called the deploy assumption and was right
about the cause, wrong about which cap would catch it.

**Before trusting any cross-arm comparison, give the harness a working deploy
path.** An arm that cannot ship cannot be told apart from an arm that ships
badly, and every arm will die on a budget cap for the same uninteresting
reason. Row 1 is what the harness looks like once that is true.
