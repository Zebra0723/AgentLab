# Predictions

Write the prediction down **before** the run, then don't edit it. The point of
the harness is to find out where your intuition about agent setups is wrong,
and a prediction you revise after seeing the log tells you nothing.

## The benchmark

| level | build | hidden tests |
| --- | --- | --- |
| easy | to-do list | add shows it · survives reload · delete removes it · empty input is harmless |
| medium | text adventure | three rooms reachable · item goes in inventory · game can be won · nonsense doesn't break it |
| hard | Pong | ball moves · paddle answers the keys · someone scores · ball never leaves the field |

Four checks per level, so a hidden-test score is always out of 4.

## What to predict

| metric | where it comes from |
| --- | --- |
| **outcome** | finished on its own, or which kill condition fired |
| **hidden tests** | x/4 |
| **SHIPPED** | URL returns 200, renders, and does not redirect off-origin |
| **messages** | assistant messages, all agents, one budget per run |
| **deploys** | deploy attempts |
| **asked a question** | did it stop and ask a human |
| **files touched vs needed** | distinct files written vs the level's expected count |
| **emoji in shipped UI** | emoji clusters in the rendered page |

## arm1 - single agent, one shot

| level | outcome | tests | SHIPPED | messages | deploys | asked? | files | emoji |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| easy | agent_done | 4/4 | PASS | 10-18 | 1-2 | no | 1-2 of 1 | 0-2 |
| medium | agent_done | 3/4 | PASS | 18-32 | 1-3 | no | 2-4 of 2 | 0-3 |
| hard | agent_done | 2/4 | PASS | 30-55 | 2-4 | no | 2-5 of 2 | 0-2 |

Reasoning, so it can be argued with:

- **easy** is a to-do list, the most written app in existence. If arm1 cannot
  do 4/4 here, suspect the harness before the agent. The only check with any
  teeth is `empty-input-is-harmless`, because guarding input is the step people
  skip.
- **medium** is where the parser bites. Three rooms and a win condition are
  easy to hand-roll; the prediction of 3/4 is that
  `nonsense-does-not-break-it` fails, because an unrecognised command is the
  path nobody tests by hand.
- **hard** is the interesting one. Pong is easy to make *look* right and hard
  to make correct: the prediction is that `ball-moves` and
  `paddle-responds-to-keys` pass, and that one of `score-increments` or
  `ball-stays-in-field` fails, because collision clamping is the classic bug.
  `window.pong` is also an unusual requirement, so an agent that skims may
  never publish state at all and score 0/4 while looking perfect on screen.
- **asked?** is predicted `no` everywhere: the prompt says nobody will answer.
  An arm that asks anyway is the interesting result.
- **emoji** is a pure guess, now that the prompt asks for deliberate design but
  still says nothing about emoji. Watch whether the number moves on its own.

### Where these are most likely wrong

- **hard could be 0/4 rather than 2/4.** If `window.pong` is not published,
  three of the four checks cannot even read the game. That is a cliff, not a
  slope, and it may make hard bimodal: near-perfect or nothing.
- **medium's message estimate is soft.** A text adventure is mostly prose; if
  the agent writes a large world it will spend more messages than predicted
  without being in any trouble.
- **files touched** assumes agents split hard and medium into the obvious
  files. A single-file Pong would score 1 of 2 and look like under-delivery
  when it is just a different shape.

## Later arms

Not built yet. Predict before building, not after.

| arm | level | outcome | tests | SHIPPED | messages | notes |
| --- | --- | --- | --- | --- | --- | --- |
| arm2 | easy | | | | | |
| arm3 | easy | | | | | |

## What to compare across arms

Not "which one was best". These four, from `python3 -m agent_lab.summary`:

1. **Ship rate (%)** - how often it ended up live
2. **Hidden tests passed (x/4)** - how much of it worked, as a score not a verdict
3. **Cost (messages)** - what it spent getting there
4. **Instruction-following** - emoji count, and whether it asked a question

An arm that ships every time with 2/4 and an arm that ships half the time with
4/4 are different animals. Flattening them into one number throws away the
finding.

## Results

Run `python3 -m agent_lab.summary` for the live table. Paste notable rows here
with what surprised you.

| run id | arm | level | outcome | tests | SHIPPED | messages | deploys | asked? | files | emoji |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| | | | | | | | | | | |

### Earlier benchmark

Before this one, the levels were a UTC clock, a tip calculator and a markdown
notepad, and the prompt said nothing about design. arm1 scored 8/8 on the clock
in 13 messages and 40 seconds, predicted correctly on all eight fields. Those
runs are **not comparable** to anything here: different tasks, different check
counts, different prompt.
