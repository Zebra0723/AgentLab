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
| | | | | | | | | | | |
