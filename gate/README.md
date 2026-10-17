# alva-gate

A guard between a trading agent and the `alva` CLI. The agent keeps calling
`alva` as before; the gate passes every command through unchanged except a
live `alva broker order place`, which it sends only when sending it cannot
plausibly buy twice.

It exists because the rules that keep an agent from double-ordering are
prose in the skill ("after an unknown outcome, retry the same intent-id or
poll, never re-place"), and the runner shows that an agent following them in
good faith can still send a second order: after a venue timeout the lookup
finds nothing yet, or a scheduled run crashes and is replayed into a session
that remembers nothing. The gate turns those rules into checks that hold
whatever the model decides.

## Rules

A live order (no `--dry-run`, no `--help`) is refused, with nothing sent, when

1. **an earlier live order on the same account, symbol and side has an
   unknown outcome.** The outcome is unknown after the CLI's exit 2, or when
   the call never returned (the host crashed). Before refusing, the gate
   looks that order up by its intent-id up to four times (pauses of 0, 1, 2
   and 4 seconds). If it turns up, the earlier order was placed and the new
   one is refused as a likely duplicate; if it still is not found, the new
   one is refused because the first may still fill;
2. **its intent-id was already placed**;
3. **in a scheduled run, the occurrence already sent a live order** on the
   same account, symbol and side. The runtime names the occurrence in
   `ALVA_GATE_OCCURRENCE`, for example `btc-daily-buy@2026-10-15T02:44:00Z`;
   the scheduler knows it, the agent does not have to;
4. it has no `--intent-id`, since its outcome could not be looked up.

A refusal comes back in the CLI's own envelope with exit 1, so the agent
reads it like a rejected order:

```json
{"schemaVersion": 2, "status": "rejected",
 "reason": {"code": "gate_outcome_unknown", "message": "alva-gate: an earlier live order ..."},
 "gate": {"earlier": "a39a6d54-..."}}
```

Codes: `gate_outcome_unknown`, `gate_earlier_order_placed`,
`gate_already_placed`, `gate_occurrence_already_placed`,
`gate_intent_required`.

Every live order is written to a journal (`journal.jsonl`) before it is sent
and again when its outcome is known; refusals and lookups go to `gate.jsonl`.
The journal belongs to the runtime, not to the agent's session: keep it
outside the agent's working directory, on storage that survives the
session. That is what lets a replayed occurrence see what the crashed one
sent. The gate also learns from the agent's own `order get` and `order
list`: an order they show is no longer unknown.

## Use

```bash
ALVA_GATE_OCCURRENCE=btc-daily-buy@2026-10-15T02:44:00Z \
  python3 gate/alva_gate.py --real "$(command -v alva)" --state /var/lib/alva-gate -- \
  broker order place --account 4471902 --venue binance --symbol BTC/USDT \
  --side buy --amount 0.01 --type market --intent-id "$(uuidgen)"
```

In practice the runtime puts a two-line `alva` script first on the agent's
PATH that execs this; the runner does exactly that for tasks named
`<task>+gate` (see [runner](../runner/README.md)).

```bash
python3 gate/test_gate.py   # 10 tests against brokersim in process
```

## What it does not do

* It never proves that an order with an unknown outcome is absent: a lookup
  that finds nothing is not proof, since the venue may still commit it. Such
  an order blocks further orders on that account, symbol and side until a
  lookup finds it. The agent is told to report the unknown outcome, so a
  run can end with the user's order in doubt rather than doubled.
* Outside a schedule it does not judge whether a second order is wanted:
  two orders the user confirmed separately both go through. That decision
  stays with the agent and the user's confirmation.
* It guards `alva broker order place` only. An order sent another way, such
  as `alva broker raw`, does not pass through it.
* One gate process per call, one journal per agent runtime; it does not
  lock across concurrent agents sharing an account.
