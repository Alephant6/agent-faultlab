# agent-faultlab

Fault-injection tests for the guardrails of an AI trading agent.

The agent under test is Alva's own skill
([alva-ai/skills](https://github.com/alva-ai/skills)) driving Alva's own CLI
([@alva-ai/toolkit](https://github.com/alva-ai/toolkit-ts)). Everything behind
the CLI is replaced by a local simulator that replays historical prices and
injects faults on a script: a reply lost after an order filled, a hung
request, a venue that commits late, rate limits, unreadable records. A checker
then reads the run's history and fill ledger, never the agent's own account of
what happened, and reports whether any user order filled twice.

The point is to test rules that exist only as prose, such as "after an
unknown outcome, retry the same intent-id or poll, never re-place", against
failures that cannot be produced on demand on a live service.

This project is not affiliated with or endorsed by Alva. It never contacts
Alva's service and contains none of its output.

## Contents

| directory | what |
|---|---|
| [brokersim/](brokersim/README.md) | the simulator: broker command grammar and trading REST routes as the CLI expects them, venue idempotency semantics as parameters, scripted faults, Jepsen-style history, checker, deterministic replay, an exploration matrix, and a smoke script that drives the real CLI |
| [market-data/](market-data/README.md) | download script for the replayed prices (Binance Vision crypto, Alpaca US stocks) and a reference SHA-256 manifest; the data itself is not committed |

## Status (2026-10-12)

* brokersim v0.1 runs with the unmodified `alva` CLI 0.31.0. Seven scenarios
  pass through the real CLI, 35 unit tests and the checker self-test pass, and
  recorded runs replay event for event.
* Next: run an LLM agent with the unmodified skill against these scenarios,
  then add a guardrail layer and compare the two.

## Quick start

```bash
cd brokersim
python3 -m brokersim fetch-data
python3 -m brokersim selftest
python3 -m brokersim serve --scenario scenarios/v2-drop-after-fill.json --out runs/v2
```

The [brokersim README](brokersim/README.md) shows how to point the CLI at it
and how to read the results.

## Licence

Code: MIT, see [LICENSE](LICENSE). Price data stays under its publishers'
terms and is downloaded by each user (crypto data: Binance Vision,
CC BY-NC-SA 4.0). `alva-ai/skills` and `alva-ai/toolkit-ts` are MIT-licensed;
no code is copied from either.
