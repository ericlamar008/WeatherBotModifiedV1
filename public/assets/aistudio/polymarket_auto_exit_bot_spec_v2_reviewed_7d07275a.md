# Polymarket Auto-Exit Bot
## Production-Oriented Technical Specification, Safety Model, Test Plan, and AI Implementation Contract

> Document type: Engineering Specification / AI Coding Contract  
> Target platform: Polymarket prediction-market CLOB  
> Primary use case: automated Stop-Loss (SL) and Take-Profit (TP) exits for temperature markets  
> Runtime target: 24/7 Linux VM, preferably Oracle Cloud Free Tier  
> Initial implementation language: Python 3.11+  
> Last technical review target: 2026-10-06  
> Important: Polymarket APIs, SDKs, order semantics, authentication, market identifiers, and platform rules can change. The implementation MUST verify the current official documentation and installed SDK at implementation/deployment time rather than trusting hard-coded assumptions in this document.

---

# 1. Executive Objective

Build an independent Auto-Exit Bot that protects and exits positions on Polymarket automatically according to user-defined Stop-Loss and Take-Profit rules.

The Bot is NOT responsible for:

- forecasting weather;
- calculating forecast probabilities;
- calculating the user's original EV;
- deciding when to open a trade;
- discovering temperature markets as a strategy;
- replacing the user's existing Weather / Paper Trading Bot;
- redeeming resolved positions unless explicitly added as a separate future feature.

The Bot IS responsible for:

1. registering or adopting positions that should be monitored;
2. storing immutable or explicitly versioned exit rules;
3. receiving real-time market data;
4. maintaining a trustworthy local representation of the relevant order book;
5. deciding whether an SL/TP condition has triggered;
6. planning a safe exit order;
7. submitting the exit order in LIVE mode;
8. reconciling the result with actual exchange/account state;
9. handling partial fills;
10. recovering safely from disconnects, process crashes, restarts, stale data, timeouts, API errors, and ambiguous order responses;
11. preventing duplicate or oversized exits;
12. providing Paper and Shadow execution paths using the SAME trigger/execution-planning logic as LIVE;
13. exposing operational status and alerts;
14. running continuously as a service.

The central design principle is:

```text
Strategy Bot
     ↓
Desired Position / Exit Rules
     ↓
Auto-Exit Bot
     ↓
Real-Time Market Data
     ↓
Trigger + Risk Validation
     ↓
Execution Planner
     ↓
LIVE / SHADOW / PAPER Executor
     ↓
Reconciliation against actual Polymarket state
```

---

# 2. Critical Architecture Distinction

There are three different concepts and they MUST NOT be conflated:

```text
A) Strategy / Paper State
B) Auto-Exit Internal Execution State
C) Actual Polymarket State
```

The Existing Weather / Paper Trading Bot may say:

```text
Paper Position:
100 shares
Entry:
0.40
SL:
0.32
TP:
0.52
```

That does NOT prove that the real Polymarket account owns 100 shares.

Therefore:

> The Auto-Exit Bot MUST NOT send a LIVE sell order against a "paper position" unless that position has explicitly been mapped/adopted as a real position and its actual quantity/identity has been verified.

For LIVE trading, actual holdings and actual fills from Polymarket are authoritative.

Recommended authority model:

```text
Existing Strategy Bot
    = desired strategy state / exit-rule source

Auto-Exit Bot
    = execution state / exit-state source

Polymarket
    = authoritative source for actual holdings, orders, fills, and market state
```

---

# 3. System Topology

Preferred deployment:

```text
                         GitHub
                 source + version control
                          │
                          │ deploy / git pull
                          ▼
                  ┌────────────────┐
                  │   Oracle VM    │
                  │                │
                  │ Existing Bot   │
                  │      │         │
                  │      │ local   │
                  │      ▼ API     │
                  │ Auto-Exit Bot  │
                  └───────┬────────┘
                          │
              ┌───────────┼─────────────┐
              │           │             │
              ▼           ▼             ▼
          Market WS    User WS       CLOB REST
              │           │             │
              └───────────┼─────────────┘
                          ▼
                      Polymarket
```

The Existing Bot and Auto-Exit Bot SHOULD run independently.

If the Existing Bot crashes:

```text
Auto-Exit Bot continues protecting already-registered live positions.
```

If the Auto-Exit Bot crashes:

```text
Existing Bot continues its own strategy operation,
but protection is temporarily unavailable.
```

The Auto-Exit Bot MUST expose its degraded state clearly.

---

# 4. Design Principles

These are non-negotiable implementation principles.

## 4.1 Separate detection from execution

```text
Market Data
   ↓
Book / Price State
   ↓
Trigger Engine
   ↓
Exit Decision
   ↓
Execution Plan
   ↓
Executor
```

The Trigger Engine MUST NOT directly place a network order.

---

## 4.2 One source of truth for actual fills

Never infer actual final position size indefinitely from local counters alone.

Local state is a cache and execution ledger.

Actual Polymarket state wins during reconciliation.

---

## 4.3 Fail closed for unintended trading

When there is uncertainty about:

- token identity;
- market identity;
- actual position size;
- order status;
- authentication;
- market state;
- tick size;
- execution price;
- duplicate order risk;

the Bot MUST NOT invent an answer.

It should move to a safe/degraded state, reconcile, or require manual intervention.

---

## 4.4 Do not silently weaken safety constraints

The Bot MUST NOT automatically:

- remove a Stop-Loss because the book is thin;
- increase maximum slippage because execution failed;
- increase maximum order size;
- change a disabled TP/SL;
- substitute a different token;
- use a stale entry price;
- reuse stale market metadata after a detected market-version/tick-size change.

Such events should produce an explicit error or alert.

---

# 5. Official Polymarket Integration Assumptions

The implementation should use Polymarket's current official Python SDK where practical.

At the time of this specification review, Polymarket's official Python repository is the unified `polymarket-client`, which combines public data, authenticated account operations, trading, and WebSocket workflows. The current release at review time is `0.12.0`, and the package requires Python >=3.11.

Use a pinned dependency version in production:

```text
polymarket-client==<verified-compatible-version>
```

Do NOT blindly use an older archived CLOB-only client if the current official SDK already provides the required workflows.

The official SDK's current architecture treats persistent WebSocket workflows as async. The Auto-Exit Bot should therefore use an asyncio-based architecture rather than wrapping WebSocket work inside synchronous polling loops.

Before implementation, the AI developer MUST inspect:

1. current official Python SDK;
2. current official WebSocket documentation;
3. current authentication documentation;
4. current order-type behavior;
5. current market/order-book metadata contracts;
6. current position/order/trade APIs.

Never invent endpoint names based only on this specification.

---

# 6. Polymarket Market Data Model

The Bot should distinguish at minimum:

```text
event
market / condition
outcome token / asset
```

The primary real-time Market WebSocket subscription key for the order book is the token / asset ID.

The authenticated User WebSocket is associated with market/condition IDs and is used for account-side order/trade lifecycle events.

Do not treat:

```text
market_id
condition_id
token_id
slug
```

as interchangeable identifiers.

Store them separately.

---

# 7. Market Metadata Required Before Monitoring

Before a position becomes LIVE-eligible, the Bot MUST validate and persist the relevant market metadata.

Minimum metadata:

```text
token_id
condition_id
market identifier/version if applicable
outcome
active
closed
resolved
enable_order_book
tick_size
neg_risk / equivalent current market-mode metadata
market end time
resolution state
```

The exact field names must follow the current SDK/API.

Metadata must be refreshed when:

- the position is first registered;
- the token is not known locally;
- a market event indicates metadata/state changed;
- a tick-size-change event is received;
- an order is rejected due to metadata/price-format issues;
- a reconciliation identifies inconsistency.

---

# 8. Tick Size Is a Safety-Critical Variable

The Bot MUST NOT assume a fixed tick size forever.

The current Polymarket WebSocket contract includes a `tick_size_change` event, and order prices must conform to the current tick size.

Therefore:

```text
Cached tick size
        ↓
may become stale
        ↓
refresh/revalidate
        ↓
round / validate order price
```

Whenever the Bot observes a tick-size change:

1. update stored metadata;
2. invalidate any cached order-construction parameters;
3. recompute the prospective order price;
4. verify the current market state before submitting.

Do not send an order using an outdated tick size.

---

# 9. Data Channels

Use both public and authenticated data paths where appropriate.

## 9.1 Market WebSocket

Use for:

- full order-book snapshots;
- order-book changes;
- best bid / best ask events where available;
- last-trade updates for observation only;
- tick-size changes;
- market-created / market-resolved events where relevant.

Market data is public and should be the primary low-latency source for trigger monitoring.

---

## 9.2 User WebSocket

Use for:

- order lifecycle;
- trade lifecycle;
- fills;
- cancellations;
- partial updates;
- account-side state transitions.

Do NOT depend exclusively on REST polling to discover fills.

REST remains necessary for reconciliation and recovery.

---

## 9.3 REST / API

Use for:

- canonical market metadata;
- current order book snapshot;
- current position/account state;
- open orders;
- order status;
- trade history;
- reconciliation;
- recovery from WebSocket gaps;
- final pre-order validation when configured.

---

# 10. WebSocket Order-Book Architecture

The Bot MUST maintain a local order-book state per monitored token.

Recommended flow:

```text
WS connection
    ↓
Initial book snapshot
    ↓
Validate
    ↓
Store local book
    ↓
Apply incremental updates
    ↓
Update best bid / best ask
    ↓
Trigger evaluation
```

A `book` event should establish a known baseline.

`price_change` or equivalent deltas update that baseline.

A zero-size price level should be removed rather than stored as active liquidity.

The AI developer MUST follow the current WebSocket contract instead of assuming a generic exchange sequence-number model.

Do NOT invent sequence-number semantics if the current API does not expose them.

If local book integrity becomes uncertain:

```text
invalidate local book
        ↓
fetch fresh REST snapshot
        ↓
replace local book
        ↓
resume monitoring
```

---

# 11. Event Freshness

Every market-data state should have:

```text
event_timestamp
receive_timestamp
age
source
```

Example:

```text
event_timestamp = exchange/event time
receive_timestamp = local monotonic/system receive time
age = now - last_valid_data_receive
```

Use a configurable stale threshold:

```text
WS_STALE_AFTER_SEC=<configured>
```

Do not hard-code an assumption that "WebSocket = always fresh".

---

# 12. Stale Data Policy

Stale market data must NOT simply disable the entire Bot.

Instead use operational states:

```text
HEALTHY
DEGRADED_MARKET_DATA
NO_MARKET_DATA
RECONCILING
```

When Market WS is stale:

1. stop treating the stale price as current;
2. attempt reconnect;
3. optionally use bounded REST snapshot fallback;
4. do not send an order using stale data;
5. keep monitoring the actual position;
6. alert if the data outage exceeds the configured threshold.

Important:

> "No new orders" and "no exit orders" are NOT the same thing.

This Bot's purpose is risk-reducing exits, so the system should not accidentally enter a mode in which it silently abandons open positions.

If no trustworthy current market data can be obtained, no exit price can be selected safely; that condition must be visible as an operational emergency.

---

# 13. Position Registration

Provide an explicit position registration/adoption workflow.

Recommended API:

```http
PUT /v1/positions/{position_id}
```

The operation SHOULD be idempotent.

Example:

```json
{
  "position_id": "pos_001",
  "source": "STRATEGY_BOT",
  "token_id": "123456",
  "condition_id": "0xabc...",
  "entry_price": "0.4000",
  "entry_price_source": "ACTUAL_FILL",
  "quantity": "100",
  "stop_loss_percent": "20",
  "take_profit_percent": "30",
  "execution_mode": "SHADOW",
  "source_version": 17
}
```

The API must NOT silently accept an unverified paper quantity for LIVE execution.

---

# 14. Position Source

Store:

```text
MANUAL
STRATEGY_BOT
EXTERNAL_BOT
IMPORTED
```

This matters for auditability and debugging.

---

# 15. Entry Price Semantics

For LIVE protection, the preferred entry-price source is:

```text
ACTUAL_FILL
```

not:

```text
PAPER_FILL
```

and not:

```text
LATEST_MARKET_PRICE
```

unless the user explicitly chooses that behavior.

If the Existing Bot has only a paper position but the user wants the Auto-Exit Bot to protect a real position, an explicit adoption workflow is required.

For example:

```text
User buys real position
      ↓
Auto-Exit Bot identifies/adopts it
      ↓
actual quantity + actual average entry are verified
      ↓
SL/TP become active
```

---

# 16. Exit Rule Model

Each monitored position should have an explicit versioned exit-rule record.

Minimum fields:

```text
rule_id
position_id
version
trigger_basis
stop_loss_enabled
stop_loss_price
stop_loss_percent
take_profit_enabled
take_profit_price
take_profit_percent
max_slippage
exit_policy
created_at
updated_at
```

Recommended:

```text
trigger_basis = GROSS_PRICE
```

for the current project.

This means:

```text
SL/TP percentage = movement of token price relative to entry price
```

rather than an estimated net P&L after fees.

Do not infer the user's actual net P&L from generic market fee metadata.

---

# 17. SL / TP Validation

For a simple long token position:

```text
0 < SL < Entry < TP < 1
```

when both are enabled.

Do not silently clamp invalid values.

Examples of invalid configurations:

```text
SL >= Entry
TP <= Entry
SL <= 0
TP >= 1
negative quantity
zero quantity
missing token_id
missing entry basis
```

The API must return a validation error.

If a user intentionally wants only one side:

```text
SL enabled, TP disabled
```

or:

```text
SL disabled, TP enabled
```

that is valid.

---

# 18. Calculating SL and TP

If:

```text
Entry = 0.40
SL = -20%
```

then:

```text
SL = 0.40 × (1 - 0.20)
   = 0.32
```

If:

```text
Entry = 0.40
TP = +30%
```

then:

```text
TP = 0.40 × (1 + 0.30)
   = 0.52
```

The system should prefer persisting the final concrete trigger prices after validation.

Percentages should remain stored for UI/audit purposes.

---

# 19. Immutable Entry Basis

After activation, the entry basis should be immutable by default.

If the user wants to change the entry basis:

```text
explicit rule update
+
new version
+
audit event
```

Do not automatically change SL/TP because a partial fill occurred.

The default model assumes that SL/TP remains anchored to the original verified entry basis.

---

# 20. Trigger Price: Use Executable Sell-Side Information

The Bot is trying to SELL.

Therefore, for a held token:

```text
sell trigger evaluation
      ↓
best executable bid
```

not merely:

```text
last traded price
```

Example:

```text
Last Trade = 0.325
Best Bid   = 0.319
SL         = 0.320
```

A last trade above SL does not protect the position if the current executable bid is already below SL.

For TP:

```text
Best Bid >= TP
```

is the natural sell-side trigger.

---

# 21. Why Last Trade Alone Is Insufficient

Last trade describes a historical execution.

It does not necessarily represent:

```text
what can be sold now
```

The Auto-Exit Bot's trigger must therefore be based on the current sell-side market state.

Last trade may still be logged as contextual information.

It should not be the primary SL/TP trigger source.

---

# 22. Trigger State Machine

Use an explicit position-level state machine.

Recommended:

```text
UNINITIALIZED
      ↓
REGISTERED
      ↓
ARMED
      ↓
EXIT_TRIGGERED
      ↓
EXECUTION_PENDING
      ↓
PARTIALLY_FILLED
      ↓
RETRY_PENDING
      ↓
CLOSED
```

Alternative terminal or exceptional states:

```text
CLOSED_EXTERNALLY
MARKET_RESOLVED
DISABLED
ERROR_NEEDS_REVIEW
```

Do not use one generic `status` field to represent both trading state and infrastructure health.

---

# 23. Separate Position State from System Health

Position state:

```text
OPEN
EXIT_TRIGGERED
PARTIAL
CLOSED
...
```

System health:

```text
HEALTHY
WS_DEGRADED
WS_DOWN
API_DEGRADED
RECONCILING
KILL_SWITCHED
...
```

These must be separate.

Example:

```text
Position = OPEN
System = WS_DOWN
```

is perfectly valid and important.

---

# 24. Exit Latching

Once an SL or TP trigger has been committed:

```text
exit_reason = STOP_LOSS
```

or:

```text
exit_reason = TAKE_PROFIT
```

the exit reason should be latched.

The Bot must NOT alternate between SL and TP after the first committed trigger because of subsequent price movement.

If the exit order partially fills or fails:

```text
same exit_reason
+
same active position
+
retry/reconcile
```

No automatic re-arming of the opposite rule.

---

# 25. Trigger Race Protection

A market update can be followed immediately by:

- another price change;
- a market resolution;
- a manual trade;
- another bot trade;
- a quantity change;
- a tick-size change.

Therefore:

```text
WS trigger
   ↓
LOCK position
   ↓
refresh / validate current state
   ↓
re-check trigger
   ↓
validate quantity
   ↓
validate market state
   ↓
plan order
   ↓
submit
```

Do not treat the original WebSocket event as an immutable truth snapshot.

---

# 26. Final Pre-Order Validation

Immediately before order submission in LIVE mode, validate:

```text
1. Bot is LIVE-enabled
2. Position still exists
3. Remaining quantity > 0
4. Token ID unchanged
5. Market is active/tradable
6. Market is not resolved/closed in a way that prevents trading
7. Current market data is fresh
8. Current order book is fresh enough
9. Trigger still holds
10. Current tick size is known
11. Current order price conforms to tick size
12. Order size <= remaining quantity
13. Order size <= configured risk limit
14. Slippage protection passes
15. No exit order is already pending
16. Kill switch is not active
17. Global risk limits are not exceeded
```

If any check fails:

```text
DO NOT SUBMIT
```

and record the exact reason.

---

# 27. Execution Price vs Trigger Price

These are different concepts.

Example:

```text
SL Trigger = 0.320
Best Bid   = 0.319
```

The Bot should record:

```text
trigger_price = 0.320
observed_executable_bid = 0.319
```

The eventual fill may differ again.

Therefore logs should distinguish:

```text
trigger price
decision-time bid
submitted worst-price limit
actual average fill
```

Never report the trigger price as the actual exit price.

---

# 28. Slippage Protection

Slippage must be an explicit execution constraint.

Example:

```text
SL trigger = 0.320
max slippage = 2%
```

Nominal minimum acceptable price:

```text
0.320 × (1 - 0.02)
= 0.3136
```

The exact submitted price must then be translated into a currently valid market tick.

Important:

> Tick-size rounding can create a trade-off between being marketable and preserving the exact decimal slippage boundary.

The implementation MUST NOT silently round in a direction that materially weakens the user's protection.

Therefore:

```text
compute protection floor
        ↓
convert to valid tick-aware price
        ↓
verify resulting order is:
  - marketable if execution is intended
  - within configured protection
```

If no valid order price can satisfy both:

```text
marketability
+
configured protection
```

the Bot should return:

```text
PROTECTION_CONSTRAINT_BLOCKED
```

rather than silently violating the constraint.

The user may configure a separate emergency-exit policy if they prefer liquidation over the protection boundary.

---

# 29. Exit Policies

Support at least:

```text
STRICT_LIMIT
MARKETABLE_FAK
MARKETABLE_FOK
```

Recommended default for SL/TP:

```text
MARKETABLE_FAK
```

because it can immediately fill available quantity and cancel the remainder.

FOK may be appropriate for a user who explicitly requires all-or-nothing execution.

GTC/GTD should NOT be the default for emergency exits because a resting order creates an ongoing state that may fill later.

If GTC/GTD is ever used:

```text
order lifecycle management
+
cancellation policy
+
reconciliation
```

become mandatory.

---

# 30. Important Polymarket Order Semantics

Current Polymarket CLOB order patterns include:

```text
GTC
GTD
FOK
FAK
```

The current platform represents marketable execution through limit-order semantics rather than assuming a generic centralized-exchange "market order" implementation.

Therefore the developer must use the current SDK's correct marketable-order construction.

Do NOT copy a generic Binance/forex market-order implementation and assume it is valid.

---

# 31. FAK Behavior

With FAK-style execution:

```text
requested quantity = 1000
immediate executable quantity = 650
```

expected outcome:

```text
filled = 650
cancelled remainder = 350
```

The remaining 350 stays in the position.

The Bot must then reconcile and decide whether/how to retry.

---

# 32. FOK Behavior

With FOK:

```text
requested quantity = 1000
immediately executable quantity < 1000
```

the order should not be treated as a partial exit.

The Bot must expect either:

```text
full fill
```

or:

```text
no fill
```

depending on current platform semantics.

---

# 33. Partial Fill Handling

Never mark a position CLOSED merely because an exit order was accepted.

Example:

```text
Original position = 1000
Exit requested    = 1000
Filled            = 650
Remaining         = 350
```

State:

```text
PARTIALLY_FILLED
remaining = 350
```

The remaining 350 should continue under the latched exit policy.

---

# 34. Preventing Duplicate Orders

This is a critical safety requirement.

Use:

```text
per-position lock
+
database transaction
+
state transition validation
+
unique exit-attempt identifier
```

Before submitting:

```text
if position has pending exit:
    do not submit another
```

If a request times out:

```text
DO NOT immediately retry
```

First reconcile actual exchange state.

Only after reconciliation should retry be considered.

---

# 35. Ambiguous Order Submission

Critical failure scenario:

```text
Bot
 ↓
POST order
 ↓
network timeout
```

The Bot does NOT know whether:

```text
order was rejected
```

or:

```text
order was accepted
```

Correct response:

```text
mark ORDER_STATUS_UNKNOWN
        ↓
query current/open orders
        ↓
query recent user-side order/trade state
        ↓
reconcile
        ↓
only then decide whether a new order is required
```

Never solve an unknown-order problem by blindly submitting another order.

---

# 36. Local Idempotency

The system must maintain a unique local execution attempt:

```text
exit_attempt_id
```

Example:

```text
exit_attempt_id = pos_001:exit:7
```

The local state should record:

```text
created_at
position_id
reason
requested_qty
planned_price
execution_mode
exchange_order_id
submission_state
```

If the current SDK/API exposes exchange-side client/order IDs, use them.

Do not invent unsupported exchange fields.

---

# 37. Order Lifecycle Tracking

At minimum, persist:

```text
local_attempt_id
exchange_order_id
position_id
token_id
side
requested_qty
filled_qty
remaining_qty
order_type
submitted_price
avg_fill_price
status
created_at
updated_at
error_code
error_message
```

User WebSocket events should update this state where available.

REST remains the recovery path.

---

# 38. Reconciliation

Reconciliation is mandatory, not optional.

Perform it:

```text
on startup
after reconnect
after ambiguous order submission
after order error
after partial fill
after manual-close detection
periodically
after relevant market-state changes
```

Recommended loop:

```text
Load local state
      ↓
Query actual account/positions
      ↓
Query open orders
      ↓
Query recent relevant trades
      ↓
Compare
      ↓
Apply deterministic reconciliation rules
      ↓
Persist new state
```

---

# 39. Reconciliation Rules

Example:

```text
Local remaining = 100
Actual token balance = 40
```

Interpretation:

```text
60 units were sold outside the current local state
```

Then:

```text
remaining = 40
status = OPEN/PARTIAL as appropriate
```

Never simply overwrite local state without logging the discrepancy.

---

# 40. External Manual Close

If the user manually sells a position:

```text
Local = OPEN
Actual = 0
```

Mark:

```text
CLOSED_EXTERNALLY
```

Do not submit another SELL.

---

# 41. External Partial Close

If the user manually sells part of the position:

```text
Local = 100
Actual = 40
```

The Bot should adopt:

```text
remaining = 40
```

and continue protecting the remainder unless the user has explicitly disabled monitoring.

---

# 42. Market Resolution and Closure

The Bot must recognize market lifecycle changes.

If the market becomes resolved:

```text
do not send additional sell orders
```

Instead:

```text
MARKET_RESOLVED
```

Then reconcile actual token state.

Do not claim the position is economically closed merely because the market resolved.

Use:

```text
MARKET_RESOLVED
    ↓
AWAITING_SETTLEMENT / RECONCILIATION
```

as needed.

Resolution is different from execution.

---

# 43. Market Paused / Order Book Disabled

If the market is no longer tradable:

```text
MARKET_UNTRADABLE
```

No normal exit order should be sent until the market becomes tradable again.

Continue reconciliation and alerts.

---

# 44. Strategy Bot ↔ Auto-Exit API

The Existing Bot should send desired configuration, not directly control execution.

Recommended endpoints:

```http
PUT /v1/positions/{position_id}
PATCH /v1/positions/{position_id}/exit-rules
POST /v1/positions/{position_id}/disable
POST /v1/positions/{position_id}/force-exit
GET /v1/positions
GET /v1/positions/{position_id}
GET /v1/health
GET /v1/metrics
```

The actual API schema must be authenticated.

---

# 45. Idempotent Position Updates

Each strategy update should contain:

```text
source_version
```

or equivalent monotonic revision.

Example:

```text
source_version = 18
```

If an old update arrives after a newer update:

```text
received version 17
current version 18
```

ignore/reject the old update.

This prevents:

```text
UI race
+
network delay
+
out-of-order requests
```

from reverting SL/TP accidentally.

---

# 46. Rule Update Race

Critical scenario:

```text
14:00:00 user sets SL = 0.32
14:00:00.100 user sets SL = 0.30
14:00:00.150 old request arrives
```

The Bot must not revert to 0.32.

Use:

```text
versioned updates
+
optimistic concurrency
```

---

# 47. API Security

Even if both Bots initially run on one Oracle VM:

Preferred:

```text
localhost only
```

Do not expose the internal control API to the public Internet.

If remote access is necessary:

```text
HTTPS
+
strong bearer/API token
+
IP/network restriction where possible
+
rate limiting
+
request audit logs
```

Do not expose administrative endpoints unauthenticated.

---

# 48. Emergency Force Exit

A manual emergency endpoint may be useful:

```http
POST /v1/positions/{position_id}/force-exit
```

It must:

1. authenticate;
2. verify the position;
3. bypass normal TP/SL trigger requirement;
4. still enforce basic safety limits;
5. create a distinct audit event;
6. reconcile after execution.

This is an operational override, not a strategy rule.

---

# 49. Execution Modes

The Bot MUST support:

```text
PAPER
SHADOW
LIVE
```

Replay is a test harness, not necessarily a permanent execution mode.

---

# 50. PAPER Mode

Paper mode must simulate order execution without sending live orders.

It should still run:

```text
same market data path
same trigger logic
same execution planning
same risk checks
```

Only:

```text
actual order submission
```

is replaced.

Paper output should include:

```text
would trigger
would submit
estimated fill
estimated slippage
```

---

# 51. SHADOW Mode

Shadow mode should consume live Polymarket market data and behave as though execution were live.

It MUST:

- maintain real order books;
- calculate real triggers;
- calculate real execution prices;
- check real tick sizes;
- calculate estimated fills;
- run real risk checks;
- produce the exact LIVE order plan.

It MUST NOT:

```text
send a real trading order
```

This is the best pre-live environment for testing real-time behavior.

---

# 52. Shadow Mode Must Be Structurally Identical to Live

Recommended design:

```python
TriggerEngine
ExecutionPlanner
RiskManager
     │
     ├── PaperExecutor
     ├── ShadowExecutor
     └── LiveExecutor
```

The core logic should NOT contain:

```python
if live:
    use different trigger logic
```

Instead only the final executor changes.

This prevents "works in shadow, breaks in live" logic divergence.

---

# 53. Replay Testing

The Bot should be able to record and replay historical market-data streams.

Recommended recorded artifacts:

```text
raw WebSocket event
receive timestamp
event timestamp
token_id
relevant REST snapshots
market metadata
test configuration
```

Storage example:

```text
replays/
    2026-10-01_market_x/
        events.jsonl
        snapshots.jsonl
        metadata.json
```

Replay speeds:

```text
0.1x
1x
10x
100x
```

The simulator should preserve event order and also support intentionally injected:

```text
duplicate events
out-of-order events
dropped events
latency
disconnects
reconnects
stale snapshots
```

---

# 54. Deterministic Execution Simulator

The test harness should contain a deterministic matching simulator.

Inputs:

```text
order book
order type
submitted size
submitted price
```

Outputs:

```text
fill quantity
average fill price
unfilled quantity
slippage
```

This allows testing:

```text
FAK
FOK
partial liquidity
thin order book
price gaps
```

without spending money.

---

# 55. Unit Tests

At minimum:

### SL below threshold

```text
Entry = 0.40
SL = -20%
Bid = 0.321
Expected = NO_TRIGGER
```

### SL exact threshold

```text
Bid = 0.320
Expected = TRIGGER
```

### SL below threshold

```text
Bid = 0.319
Expected = TRIGGER
```

### TP below threshold

```text
Entry = 0.40
TP = +30%
Bid = 0.519
Expected = NO_TRIGGER
```

### TP exact threshold

```text
Bid = 0.520
Expected = TRIGGER
```

### TP above threshold

```text
Bid = 0.530
Expected = TRIGGER
```

---

# 56. Configuration Validation Tests

Test:

```text
SL >= Entry
TP <= Entry
SL <= 0
TP >= 1
negative percentage
negative quantity
zero quantity
unknown token
missing tick size
missing entry basis
```

All invalid inputs must fail safely.

---

# 57. Order Book Tests

Test:

```text
empty book
single bid level
multiple bid levels
zero-size removal
partial depth
stale snapshot
book replacement after reconnect
tick-size change
```

---

# 58. Partial Fill Tests

Example:

```text
Position = 1000
FAK fill = 650
```

Expected:

```text
Remaining = 350
Position != CLOSED
Exit rule remains latched
```

---

# 59. Duplicate Trigger Tests

Generate 100 identical trigger events.

Expected:

```text
at most one active exit attempt
```

before reconciliation determines the outcome.

---

# 60. Restart Tests

Simulate:

```text
process crash
```

at every state:

```text
before trigger
after trigger
before order submission
after order submission
after timeout
after partial fill
after full fill
```

Restart must lead to:

```text
reconcile
resume
```

not:

```text
blindly sell again
```

---

# 61. Ambiguous Network Failure Tests

Simulate:

```text
order request accepted by exchange
response packet lost
```

Expected:

```text
ORDER_STATUS_UNKNOWN
        ↓
reconcile
        ↓
discover actual order/fill
        ↓
no duplicate order
```

---

# 62. WebSocket Failure Tests

Simulate:

```text
disconnect
reconnect
duplicate subscription
missing events
stale data
```

Expected:

```text
no corrupted local order book
no duplicate exits
fresh book before resuming normal operation
```

---

# 63. Tick-Size Change Tests

Simulate:

```text
tick size old = 0.01
tick size new = 0.001
```

and the reverse.

Expected:

```text
cached order-price assumptions invalidated
```

The Bot must regenerate the execution plan.

---

# 64. Market Resolution Tests

Simulate:

```text
SL trigger arrives
market_resolved event arrives immediately afterward
```

The final pre-order validation must prevent a stale order from being sent if the market is no longer tradable.

---

# 65. Manual Intervention Tests

Test:

```text
user sells entire position
user sells half
user disables rules
user changes SL
user changes TP
user force-exits
```

The Bot must reconcile and respect the latest versioned instruction.

---

# 66. Strategy/Data Mismatch Tests

Example:

```text
Paper Bot:
100 shares @ 0.40

Polymarket:
0 shares
```

Expected:

```text
NO LIVE SELL
```

Another case:

```text
Paper Bot:
100 shares

Polymarket:
40 shares
```

Expected:

```text
protect only the actual 40
+
audit discrepancy
```

---

# 67. Risk Management

Required global risk limits:

```text
MAX_ORDER_SIZE
MAX_POSITION_SIZE
MAX_TOTAL_EXPOSURE
MAX_CONCURRENT_EXIT_ORDERS
MAX_DAILY_LOSS
MAX_SLIPPAGE
MAX_RETRY_COUNT
```

The risk engine MUST evaluate actual current exposure, not only local intended exposure.

---

# 68. Per-Market Allowlist

LIVE mode should support an allowlist:

```text
ALLOWED_TOKEN_IDS
```

or equivalent market identifiers.

If a position is not explicitly registered:

```text
do not trade
```

This prevents a software bug from accidentally issuing orders against unrelated assets.

---

# 69. Live Trading Guard

Require an explicit LIVE activation.

Example conceptual configuration:

```text
EXECUTION_MODE=LIVE
LIVE_TRADING_ENABLED=true
```

Recommended additional safety:

```text
LIVE_CONFIRMATION=<explicit human-controlled value>
```

The goal is to make an accidental switch from:

```text
SHADOW
```

to:

```text
LIVE
```

difficult.

---

# 70. Kill Switch

Global kill switch:

```text
TRADING_KILL_SWITCH=true
```

means:

```text
do not submit new live orders
```

Monitoring and reconciliation may continue.

The system should alert:

```text
KILL SWITCH ACTIVE
```

and should NOT silently appear healthy.

---

# 71. Emergency-Exit Policy

Because a strict slippage limit can block execution in a rapidly moving/thin market, make this explicit.

Possible policies:

```text
STRICT
```

Do not cross configured protection.

```text
CONTROLLED_EMERGENCY
```

Allow an independently configured emergency limit after a defined condition.

```text
DISABLED
```

Do not use this in production unless explicitly understood.

Do NOT silently convert a strict user setting into an emergency liquidation rule.

---

# 72. Retry Policy

Retries must be state-aware.

Do NOT:

```text
every price tick → send another order
```

Instead:

```text
attempt
 ↓
reconcile
 ↓
if remainder exists and trigger still active:
    retry according to backoff/risk rules
```

Retry controls:

```text
MAX_EXIT_ATTEMPTS
RETRY_BACKOFF
RETRY_IF_TRIGGER_STILL_ACTIVE
```

Rate limits must always be respected.

---

# 73. Retry Classification

Errors should be classified.

Examples:

```text
TRANSIENT_NETWORK
RATE_LIMIT
STALE_BOOK
INVALID_PRICE
INVALID_TICK_SIZE
MARKET_UNTRADABLE
INSUFFICIENT_POSITION
AUTH_ERROR
PERMISSION_ERROR
UNKNOWN_ORDER_STATE
```

Only genuinely transient failures should be automatically retried.

Configuration/programming errors should fail safely and alert.

---

# 74. Authentication and Security

The current Polymarket CLOB authentication model includes:

```text
L1:
wallet/private-key signing

L2:
API credentials / HMAC authentication
```

Order creation involves signing the order payload.

Therefore:

> The private key is a high-value security boundary.

Never put the private key in:

```text
GitHub
README
source code
logs
Telegram messages
database rows
exception traces
```

---

# 75. Wallet Security

Use a dedicated trading wallet.

Do NOT use the user's primary wallet for an automated experimental bot.

Recommended:

```text
small capital
+
dedicated wallet
+
limited market allowlist
+
strict order limits
```

---

# 76. Important Correction to the "No Withdrawal" Claim

The application itself should expose:

```text
NO withdrawal feature
```

However, it is technically incorrect to claim:

```text
"the bot can never withdraw"
```

merely because withdrawal code is absent.

If a full private key is compromised, an attacker may potentially sign other transactions outside the intended Bot logic.

Therefore real security relies on:

```text
dedicated wallet
+
minimal capital
+
strong host security
+
secret isolation
+
least privilege where supported
+
no unnecessary signing capabilities
```

If a delegated/scoped signing mechanism is used, its current support and exact permissions must be verified against the current Polymarket platform before relying on it.

---

# 77. Secret Storage on Oracle

Do not store secrets in the Git repository.

Prefer a server-side secrets file or secure secret mechanism.

Example conceptual layout:

```text
/etc/auto-exit-bot/secrets.env
```

with restrictive filesystem permissions.

The service should run under a dedicated Linux user:

```text
autoexit
```

Do not run the Bot as root.

---

# 78. GitHub Security

Repository may be public if desired.

Allowed:

```text
source code
tests
documentation
configuration templates
```

Forbidden:

```text
private key
wallet seed
CLOB secret
CLOB passphrase
API tokens
real .env
production database
session credentials
```

Use `.gitignore`.

Run secret scanning in CI.

---

# 79. Supply-Chain Security

Use a pinned dependency lock file.

Before dependency upgrades:

```text
run unit tests
run replay tests
run integration tests
run Shadow mode
```

Do not upgrade the Polymarket SDK directly on the production server without validation.

Recommended:

```text
development
   ↓
CI
   ↓
staging/shadow
   ↓
production
```

---

# 80. Oracle VM Security

Recommended:

```text
SSH key authentication
disable password SSH where practical
dedicated bot user
firewall
only required inbound ports
automatic security updates where appropriate
NTP/time synchronization
```

The Existing Bot ↔ Auto-Exit control API should stay private/local if possible.

---

# 81. Systemd Deployment

Run Auto-Exit as a persistent systemd service.

Conceptual:

```text
systemd
   ↓
auto-exit-bot.service
   ↓
python -m auto_exit_bot
```

The service should:

```text
Restart=always
```

with sensible restart throttling.

Expected behavior:

```text
Bot crashes
   ↓
systemd restarts
   ↓
Bot loads DB
   ↓
reconciliation
   ↓
reconnect WS
   ↓
resume
```

---

# 82. Graceful Shutdown

On SIGTERM/SIGINT:

1. stop accepting new strategy updates;
2. stop starting new exit attempts;
3. let active network operations reach a safe checkpoint where possible;
4. persist state;
5. close WebSocket cleanly;
6. exit.

Do NOT cancel an already-submitted exchange order merely because the process is shutting down unless the operational policy explicitly requires that behavior.

On next startup, reconcile first.

---

# 83. Database

SQLite is acceptable for the initial single-VM architecture.

Recommended:

```text
WAL mode
foreign keys enabled
transactional writes
busy timeout
periodic backup
```

Suggested tables:

```text
positions
exit_rules
orders
fills
market_metadata
book_state
market_events
bot_events
health_events
configuration_versions
```

---

# 84. Database Invariants

Important constraints:

```text
position_id unique
active exit attempt per position <= 1
remaining_quantity >= 0
filled_quantity >= 0
filled_quantity <= requested_quantity
version monotonically increasing
```

Use database constraints where possible rather than relying only on Python code.

---

# 85. Decimal Arithmetic

Financial values must NOT use binary float arithmetic.

Use:

```python
Decimal
```

for:

```text
price
quantity
percentage
slippage
fill value
```

Convert to/from API strings carefully.

For example:

```python
from decimal import Decimal
```

Do not use:

```python
float
```

for safety-critical threshold comparisons.

---

# 86. Order-Book Quantity Arithmetic

When estimating a sell:

```text
Position = 500
```

and bids:

```text
100 @ 0.320
200 @ 0.319
300 @ 0.318
```

the simulator should calculate:

```text
100 @ 0.320
200 @ 0.319
200 @ 0.318
```

and:

```text
filled_quantity = 500
```

with weighted average fill:

```text
Σ(price × quantity) / Σ(quantity)
```

Use Decimal arithmetic.

---

# 87. Execution Planner

Create a dedicated component:

```text
ExecutionPlanner
```

Input:

```text
position
exit rule
current book
tick size
risk limits
market state
```

Output:

```text
ExecutionPlan
```

Example:

```json
{
  "position_id": "pos_001",
  "reason": "STOP_LOSS",
  "quantity": "100",
  "observed_best_bid": "0.319",
  "trigger_price": "0.320",
  "worst_acceptable_price": "0.3136",
  "selected_order_price": "...",
  "order_type": "FAK"
}
```

The planner must explain why it selected or rejected a plan.

---

# 88. Trigger Engine

The Trigger Engine should be pure or as close to pure as practical.

Input:

```text
entry_price
SL rule
TP rule
best executable bid
```

Output:

```text
NO_TRIGGER
STOP_LOSS
TAKE_PROFIT
INVALID_RULE
```

It must NOT:

- access the network;
- mutate exchange state;
- send orders.

This makes testing easy.

---

# 89. Risk Manager

The Risk Manager must operate before execution.

Input:

```text
execution plan
actual position
global exposure
limits
market state
```

Output:

```text
ALLOW
BLOCK
BLOCK_REASON
```

Possible reasons:

```text
MAX_ORDER_SIZE
MAX_EXPOSURE
MAX_SLIPPAGE
NO_POSITION
MARKET_UNTRADABLE
KILL_SWITCH
STALE_DATA
UNKNOWN_STATE
```

---

# 90. Execution Interface

Recommended interface:

```python
class Executor:
    async def submit_exit(plan) -> ExecutionResult:
        ...
```

Implement:

```text
PaperExecutor
ShadowExecutor
LiveExecutor
```

All should return a normalized result.

---

# 91. Live Executor Responsibilities

LiveExecutor alone should know how to:

```text
authenticate
build signed order
submit order
retrieve status
cancel if required
```

The strategy/trigger code should not know SDK-specific request formats.

This isolates future Polymarket API changes.

---

# 92. SDK Adapter Layer

Create:

```text
polymarket/
    client.py
    market_data.py
    trading.py
    account.py
    websocket.py
```

Do NOT scatter direct SDK calls across the application.

This makes SDK upgrades safer.

---

# 93. User WebSocket as Fill Confirmation

After a live order:

```text
LiveExecutor
     ↓
order submitted
     ↓
User WS event(s)
     ↓
order/trade state updated
```

REST reconciliation remains authoritative after ambiguity or restart.

---

# 94. Market WebSocket Subscription Management

The Bot should dynamically subscribe/unsubscribe as positions become active/inactive.

When a position is created:

```text
subscribe token
```

When no monitored position needs the token:

```text
unsubscribe token
```

Avoid unnecessary subscriptions.

If multiple positions share the same token:

```text
one market subscription
+
multiple local position watchers
```

---

# 95. One Token Can Map to Multiple Internal Positions

The system should not assume:

```text
1 token = 1 position record
```

A user could have multiple logical entries against the same token.

The market data layer should publish:

```text
token market state
```

and the position layer should decide which position rules apply.

However, the execution layer must aggregate or coordinate quantities to prevent total sells from exceeding the actual token balance.

---

# 96. Aggregate Quantity Safety

Suppose:

```text
Position A = 60
Position B = 50
Actual token balance = 80
```

The Bot must not send:

```text
60 + 50 = 110
```

in aggregate.

Before submission, reserve against:

```text
actual balance
+
currently pending exit orders
```

where applicable.

A token-level lock/reservation mechanism is recommended.

---

# 97. Concurrency Model

Use asyncio for I/O.

Use:

```text
per-position lock
per-token lock
database transaction
```

to avoid concurrency hazards.

Example:

```text
WS event A → trigger position 1
WS event B → trigger position 1
REST reconciliation → update position 1
UI rule update → update position 1
```

These operations must not corrupt one another.

---

# 98. Async Architecture

Recommended conceptual architecture:

```text
asyncio
├── market_ws_task
├── user_ws_task
├── reconciliation_task
├── position_sync_task
├── alert_task
├── health_task
└── API_server_task
```

Use queues where appropriate:

```text
WebSocket event
      ↓
async queue
      ↓
market state processor
      ↓
trigger processor
```

Avoid uncontrolled background tasks that cannot be awaited or cancelled.

---

# 99. Event Ordering

Network events may arrive with different receive times.

Store:

```text
event timestamp
receive timestamp
```

Use the API's actual event semantics.

Do not blindly assume that local receive order always equals exchange causal order.

If a later reconciliation contradicts an earlier local event:

```text
reconciliation wins
```

and the discrepancy must be logged.

---

# 100. Health Model

Expose:

```text
overall status
market WS status
user WS status
REST status
database status
last successful reconciliation
last market-data timestamp
last execution timestamp
```

Example:

```json
{
  "status": "DEGRADED",
  "market_ws": "CONNECTED",
  "user_ws": "DISCONNECTED",
  "rest": "OK",
  "database": "OK",
  "last_reconciliation": "...",
  "last_market_event": "..."
}
```

---

# 101. Structured Logging

Every significant event should contain:

```text
timestamp
level
event_type
position_id
token_id
condition_id
attempt_id
execution_mode
reason
```

Examples:

```text
POSITION_REGISTERED
RULE_UPDATED
TRIGGER_DETECTED
EXECUTION_PLAN_CREATED
ORDER_SUBMITTED
ORDER_STATUS_UNKNOWN
ORDER_PARTIAL_FILL
ORDER_FILLED
RECONCILIATION_ADJUSTMENT
MARKET_RESOLVED
WEBSOCKET_RECONNECTED
KILL_SWITCH_ACTIVATED
```

---

# 102. Never Log Secrets

Logs must never contain:

```text
private key
seed phrase
API secret
API passphrase
bearer tokens
raw authentication headers
```

Sanitize exception objects and request debugging output.

---

# 103. Alerts

At minimum, alert on:

```text
Bot startup
Bot crash/restart
WebSocket outage
User WS outage
reconciliation mismatch
order failure
ambiguous order
partial-fill retry
market resolved
kill switch
risk block
authentication failure
database failure
```

Successful routine ticks should not spam alerts.

---

# 104. Telegram Alert Example

Example conceptual message:

```text
STOP LOSS EXECUTED

Market: <market>
Token: <token>
Position: <id>

Entry: 0.400
SL: 0.320
Trigger Bid: 0.319

Requested: 100
Filled: 100
Average Exit: 0.319

Mode: LIVE
Status: CLOSED
```

Do not include sensitive credentials.

---

# 105. Monitoring Metrics

Recommended metrics:

```text
positions_open
positions_protected
trigger_count_sl
trigger_count_tp
exit_success_count
exit_partial_count
exit_failure_count
ambiguous_order_count
reconciliation_mismatch_count
websocket_reconnect_count
market_data_age
order_submission_latency
average_slippage
blocked_order_count
risk_limit_hits
```

---

# 106. Cost / Resource Design

This is a lightweight 24/7 service.

Avoid unnecessary:

```text
high-frequency REST polling
large in-memory history
heavy ML models
CPU-intensive processing
```

Use WebSocket for streaming.

Use REST for:

```text
bootstrap
fallback
reconciliation
metadata
recovery
```

---

# 107. Polling Strategy

Do NOT implement:

```text
check price every N minutes
```

as the primary SL/TP mechanism.

Use:

```text
WebSocket-driven trigger monitoring
```

with configurable REST recovery/reconciliation.

A reconciliation interval may be, for example:

```text
15–60 seconds
```

depending on API limits and desired redundancy.

This interval is NOT a replacement for real-time monitoring.

---

# 108. Time Synchronization

The Oracle VM must maintain accurate system time.

Reason:

- authentication timestamps;
- event correlation;
- logging;
- replay;
- timeout handling.

Use OS time synchronization.

Do not manually manipulate the clock.

---

# 109. Rate Limit Handling

All REST calls must pass through a rate-aware client.

On rate limit:

```text
read Retry-After / SDK rate-limit state if available
        ↓
backoff
        ↓
retry only when appropriate
```

Never spin in a tight loop.

---

# 110. Market Metadata Cache

Cache:

```text
tick size
market status
token mapping
condition ID
market flags
```

But treat cached metadata as:

```text
cache
```

not as permanent truth.

Invalidate it when the market emits relevant changes or when an order/API error indicates it is stale.

---

# 111. Source of Truth Matrix

| Data | Preferred Authority |
|---|---|
| Actual position quantity | Polymarket |
| Actual fills | Polymarket User WS + reconciliation |
| Actual open orders | Polymarket |
| Market status | Polymarket current market metadata / events |
| Current book | Market WS + REST snapshot recovery |
| SL/TP user configuration | Auto-Exit DB + versioned source update |
| Strategy forecast | Existing Strategy Bot |
| Execution state | Auto-Exit DB, reconciled with Polymarket |

---

# 112. What the Existing Bot Should NOT Do

The Existing Bot should not directly issue live SELL orders on behalf of Auto-Exit.

It should communicate:

```text
which position should be monitored
what exit rules should apply
whether monitoring should be enabled/disabled
```

This avoids duplicated execution logic.

---

# 113. What the Auto-Exit Bot Should NOT Do

Auto-Exit should not:

```text
open positions
change forecast
calculate temperature probabilities
reprice strategy entries
decide EV
invent positions
sell unrelated tokens
```

unless future scope explicitly adds these features.

---

# 114. Operational Commands

Useful CLI commands:

```text
auto-exit status
auto-exit positions
auto-exit reconcile
auto-exit health
auto-exit test-trigger
auto-exit replay
auto-exit shadow-report
auto-exit enable
auto-exit disable
```

A command that enables LIVE mode should require explicit configuration and should show a warning.

---

# 115. Example End-to-End SL Scenario

```text
Real position:
100 shares
actual entry = 0.400
SL = 0.320

Market WS:
best_bid = 0.325

No trigger.
```

Then:

```text
best_bid changes to 0.319
```

Flow:

```text
WS event
 ↓
update order book
 ↓
TriggerEngine → STOP_LOSS
 ↓
latch exit reason
 ↓
position lock
 ↓
freshness validation
 ↓
REST/book validation if required
 ↓
tick-size validation
 ↓
risk validation
 ↓
ExecutionPlanner
 ↓
FAK SELL plan
 ↓
LiveExecutor
 ↓
User WS / REST confirmation
 ↓
reconciliation
 ↓
CLOSED or PARTIAL
```

---

# 116. Example TP Scenario

```text
Entry = 0.400
TP = 0.520
Best Bid = 0.519
```

No trigger.

Then:

```text
Best Bid = 0.521
```

Trigger:

```text
TAKE_PROFIT
```

The same safety pipeline applies.

---

# 117. Example Partial Fill Scenario

```text
Position = 1000
TP = 0.520
Best bid depth:
500 @ 0.521
100 @ 0.520
```

FAK execution may produce:

```text
Filled = 600
Remaining = 400
```

The Bot:

```text
does not declare CLOSED
does not switch reason
does not sell 1000 again
```

It reconciles and handles the 400 remaining according to configured retry policy.

---

# 118. Example Ambiguous Response Scenario

```text
SL triggers
order request sent
connection timeout
```

Correct:

```text
state = ORDER_STATUS_UNKNOWN
       ↓
query actual orders/trades
       ↓
if fill found:
    update state
if no order found:
    evaluate retry
if data is inconsistent:
    ERROR_NEEDS_REVIEW
```

Incorrect:

```text
timeout
↓
send another sell immediately
```

---

# 119. Example Restart During Exit

State before crash:

```text
EXIT_TRIGGERED
ORDER_SUBMISSION_PENDING
```

After restart:

```text
load DB
↓
detect incomplete attempt
↓
reconcile exchange
↓
if order exists:
    track it
if order filled:
    update position
if no order exists:
    re-evaluate retry eligibility
```

Never assume the process crash cancelled the exchange action.

---

# 120. Example Market Resolution Race

```text
SL trigger
      ↓
before submission:
market resolves
```

Final pre-order validation:

```text
market is no longer tradable
```

Therefore:

```text
NO ORDER
MARKET_RESOLVED
```

and reconcile the position.

---

# 121. Example Rule Update Race

```text
Version 12:
SL = 0.32

Version 13:
SL = 0.31
```

If version 12 arrives after version 13:

```text
ignore stale update
```

If a trigger is already latched under version 13:

```text
do not silently replace the active exit state
```

Unless the user explicitly performs a supported override.

---

# 122. Testing Levels

Use this progression:

```text
1. Unit tests
2. Property / invariant tests
3. Deterministic order-book simulation
4. Historical replay
5. Fault injection
6. Live market Shadow mode
7. Tiny-capital live
8. Production live
```

Do not skip Shadow because the code "looks correct."

---

# 123. Property / Invariant Tests

Examples:

### Never oversell

For every test:

```text
total live sell quantity
<=
verified actual quantity
```

### Never submit twice concurrently

For every position:

```text
active_exit_attempts <= 1
```

### No false closure

A position is CLOSED only when:

```text
actual remaining quantity == 0
```

or when a verified terminal state makes trading impossible and the state is explicitly classified.

### No invalid price

Every live order price:

```text
conforms to current tick size
```

### No stale-order submission

A live order must not be submitted if required market data exceeds the configured freshness limit.

---

# 124. Fault Injection

Add test switches to simulate:

```text
WS disconnect
REST timeout
order timeout
order reject
partial fill
rate limit
stale book
tick-size change
market resolution
duplicate event
out-of-order event
database lock
process crash
manual close
rule update race
```

---

# 125. Shadow Metrics

After Shadow testing, calculate:

```text
number of triggers
expected exit count
estimated fill rate
estimated average slippage
partial-fill frequency
blocked execution count
stale-data incidents
reconnect incidents
duplicate-trigger incidents
```

A Shadow deployment should run long enough to expose normal reconnects, sparse books, and market-state transitions.

---

# 126. Tiny-Live Deployment

When Shadow is stable:

```text
dedicated wallet
+
very small capital
+
one or few positions
+
strict order-size cap
```

Monitor manually.

Do not immediately scale to all markets.

---

# 127. Production Rollout

Recommended rollout:

```text
one token
  ↓
one market
  ↓
small number of markets
  ↓
full temperature-market set
```

Increase coverage only after observing real execution behavior.

---

# 128. Python Project Structure

Recommended:

```text
auto-exit-bot/
│
├── README.md
├── pyproject.toml
├── uv.lock / lockfile
├── .gitignore
├── .env.example
│
├── src/
│   └── auto_exit/
│       ├── __init__.py
│       ├── main.py
│       │
│       ├── api/
│       │   ├── routes_positions.py
│       │   ├── routes_health.py
│       │   └── auth.py
│       │
│       ├── config/
│       │   └── settings.py
│       │
│       ├── domain/
│       │   ├── models.py
│       │   ├── enums.py
│       │   └── state_machine.py
│       │
│       ├── market/
│       │   ├── ws_market.py
│       │   ├── ws_user.py
│       │   ├── orderbook.py
│       │   └── metadata.py
│       │
│       ├── strategy/
│       │   └── trigger_engine.py
│       │
│       ├── execution/
│       │   ├── planner.py
│       │   ├── base.py
│       │   ├── paper.py
│       │   ├── shadow.py
│       │   └── live.py
│       │
│       ├── polymarket/
│       │   ├── adapter.py
│       │   ├── account.py
│       │   ├── market.py
│       │   ├── orders.py
│       │   └── websocket.py
│       │
│       ├── risk/
│       │   └── risk_manager.py
│       │
│       ├── reconciliation/
│       │   └── reconciler.py
│       │
│       ├── storage/
│       │   ├── database.py
│       │   ├── repositories.py
│       │   └── migrations.py
│       │
│       ├── replay/
│       │   ├── recorder.py
│       │   ├── player.py
│       │   └── simulator.py
│       │
│       └── monitoring/
│           ├── logging.py
│           ├── metrics.py
│           └── alerts.py
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── replay/
│   ├── fault_injection/
│   └── fixtures/
│
└── deploy/
    ├── systemd/
    ├── oracle/
    └── README.md
```

---

# 129. Development Order

Do NOT build all components simultaneously.

Implement in this order:

## Phase 1 — Domain

- Position model
- Exit-rule model
- State machine
- Decimal arithmetic
- validation

## Phase 2 — Trigger Engine

- SL
- TP
- latching
- invalid-rule handling

## Phase 3 — Order-Book Engine

- full book
- deltas
- best bid
- depth
- simulation

## Phase 4 — Risk Manager

- order limits
- position limits
- slippage
- allowlist
- kill switch

## Phase 5 — Paper Executor

- deterministic simulated execution
- persistence

## Phase 6 — Replay

- recorder
- replay
- latency injection
- dropped/duplicate/out-of-order events

## Phase 7 — Polymarket Public Data

- Market WS
- REST snapshots
- metadata
- tick-size handling

## Phase 8 — Shadow

- real market data
- no real orders

## Phase 9 — Account / User Data

- positions
- open orders
- user WS
- fills

## Phase 10 — Reconciliation

- startup
- periodic
- post-error
- post-reconnect

## Phase 11 — Live Executor

Only after all previous phases pass.

## Phase 12 — Oracle Deployment

- systemd
- service user
- secret file
- firewall
- health monitoring

## Phase 13 — Tiny Live

## Phase 14 — Production

---

# 130. Definition of Done

LIVE is NOT considered production-ready until all of the following are true:

```text
[ ] domain tests pass
[ ] trigger tests pass
[ ] order-book tests pass
[ ] price/tick tests pass
[ ] partial-fill tests pass
[ ] duplicate-trigger tests pass
[ ] ambiguous-order tests pass
[ ] restart tests pass
[ ] reconnect tests pass
[ ] stale-data tests pass
[ ] tick-size-change tests pass
[ ] resolution-race tests pass
[ ] manual-close tests pass
[ ] rule-version race tests pass
[ ] risk-limit tests pass
[ ] kill-switch tests pass
[ ] replay tests pass
[ ] fault-injection tests pass
[ ] Shadow mode is stable
[ ] secrets are outside Git
[ ] dedicated trading wallet is used
[ ] LIVE guard is explicit
[ ] systemd restarts process
[ ] database persists state
[ ] startup reconciliation is mandatory
[ ] logs are structured
[ ] alerts work
```

---

# 131. Non-Goals for Version 1

Do NOT add unnecessary complexity such as:

```text
machine-learning prediction
automatic forecast strategy
portfolio optimization
automatic market discovery
automatic deposits
withdrawals
on-chain redeem workflows
multi-wallet fleet management
```

The first production objective is:

```text
reliable automated exit execution
```

---

# 132. Security Threat Model

Consider at least these threats:

## Threat A — GitHub secret leak

Mitigation:

```text
no secrets in repo
secret scanning
.env ignored
```

## Threat B — Oracle compromise

Mitigation:

```text
dedicated wallet
limited funds
dedicated OS user
firewall
minimal exposed services
```

## Threat C — Application bug

Mitigation:

```text
risk limits
allowlist
Decimal
state machine
locks
reconciliation
tiny-capital testing
```

## Threat D — Duplicate order

Mitigation:

```text
locks
persistent attempt state
reconciliation
unknown-order workflow
```

## Threat E — Stale market data

Mitigation:

```text
freshness threshold
WS reconnect
REST fallback
pre-order validation
```

## Threat F — API behavior changes

Mitigation:

```text
SDK adapter
version pinning
integration tests
documentation re-verification
```

---

# 133. Important Implementation Anti-Patterns

The AI developer MUST avoid:

```python
while True:
    price = get_price()
    time.sleep(60)
```

as the core SL/TP architecture.

Also avoid:

```python
if price <= stop_loss:
    sell()
```

without:

```text
position verification
market-data freshness
order lock
tick-size validation
risk validation
reconciliation
```

Also avoid:

```python
try:
    sell()
except:
    sell_again()
```

This is unsafe.

Also avoid:

```python
position.closed = True
```

immediately after submitting an order.

Closure must be based on verified execution state.

---

# 134. AI Coding-Agent Rules

When an AI coding agent receives this document, it should follow these rules:

### Rule 1

Inspect the existing project before writing integration code.

### Rule 2

Do not invent Polymarket endpoints.

### Rule 3

Verify current official SDK interfaces.

### Rule 4

Prefer adapter layers over direct SDK calls throughout the codebase.

### Rule 5

Implement tests before Live execution.

### Rule 6

Never use real credentials in tests.

### Rule 7

Default to SHADOW, not LIVE.

### Rule 8

Never enable LIVE implicitly.

### Rule 9

Never place a LIVE order before final validation.

### Rule 10

Never assume a timeout means the order failed.

### Rule 11

Never assume local position quantity is authoritative.

### Rule 12

Never use stale order-book data for a fresh execution decision.

### Rule 13

Never change user safety constraints silently.

### Rule 14

Never hard-code tick sizes or market identifiers.

### Rule 15

Use Decimal for financial calculations.

### Rule 16

All dangerous operations must produce structured audit logs.

### Rule 17

Any uncertain exchange state should trigger reconciliation.

### Rule 18

When unsure, fail closed rather than guessing.

---

# 135. Required AI Development Workflow

For each implementation task:

```text
1. inspect relevant current code
2. identify interfaces
3. identify invariants
4. implement smallest change
5. write/update tests
6. run tests
7. inspect logs / failures
8. only then integrate with next layer
```

Do not perform a giant one-shot rewrite unless explicitly requested.

---

# 136. Acceptance Scenarios

The completed Bot must correctly handle all of these:

```text
Scenario 1:
SL triggers and full FAK fill occurs.

Scenario 2:
SL triggers and only half the position fills.

Scenario 3:
TP triggers and full fill occurs.

Scenario 4:
Order submission times out but order actually exists.

Scenario 5:
Order submission times out and no order exists.

Scenario 6:
WebSocket disconnects just before trigger.

Scenario 7:
WebSocket disconnects immediately after trigger.

Scenario 8:
Bot crashes after exchange accepted the order.

Scenario 9:
User manually closes the position.

Scenario 10:
User manually closes half the position.

Scenario 11:
Tick size changes.

Scenario 12:
Market resolves during execution race.

Scenario 13:
Existing Bot sends stale rule update.

Scenario 14:
Two identical trigger events arrive simultaneously.

Scenario 15:
Two logical positions share one token.

Scenario 16:
Actual token balance is smaller than local intended quantity.

Scenario 17:
Order book becomes too thin for configured slippage.

Scenario 18:
Market data becomes stale.

Scenario 19:
REST API rate limit occurs.

Scenario 20:
Oracle VM restarts.

Scenario 21:
Database temporarily locks.

Scenario 22:
Kill switch is activated while a position is open.

Scenario 23:
Bot starts with positions already open.

Scenario 24:
Bot starts while an old exit order is already open.

Scenario 25:
Bot starts after a previous process died with an unknown order state.
```

---

# 137. Expected Behavior During Ambiguity

Whenever there is uncertainty:

```text
unknown position
unknown order
unknown market state
stale data
invalid tick
```

the Bot should prefer:

```text
NO UNINTENDED ORDER
+
RECONCILIATION
+
ALERT
```

over:

```text
GUESS
+
TRADE
```

This principle is more important than raw execution speed.

---

# 138. Performance Target

The system is not required to be ultra-low-latency HFT software.

Target:

```text
real-time event consumption
low milliseconds-to-low hundreds of milliseconds
```

depending on network and exchange response.

Correctness and state safety are higher priorities than microsecond optimization.

Do not introduce complicated performance optimizations prematurely.

---

# 139. Reliability Target

Desired production property:

```text
Bot may restart,
WebSocket may reconnect,
network may fail,
orders may partially fill,
and the system should recover without overselling.
```

This is the key reliability objective.

---

# 140. Final Architecture

```text
                           EXISTING BOT
                  ┌─────────────────────────┐
                  │ Forecast / EV / UI      │
                  │ Strategy / Paper state  │
                  └────────────┬────────────┘
                               │
                       versioned API
                               │
                               ▼
                  ┌─────────────────────────┐
                  │      AUTO-EXIT BOT      │
                  │                         │
                  │  Position Registry      │
                  │  Rule Store             │
                  │  Market Metadata        │
                  │  Order Book Engine      │
                  │  Trigger Engine          │
                  │  Risk Manager            │
                  │  Execution Planner      │
                  │  Executor               │
                  │  Reconciler             │
                  │  Persistence             │
                  │  Monitoring / Alerts     │
                  └────────────┬────────────┘
                               │
                   ┌───────────┼─────────────┐
                   │           │             │
                   ▼           ▼             ▼
              Market WS    User WS       CLOB REST
                   │           │             │
                   └───────────┼─────────────┘
                               ▼
                         POLYMARKET
```

Execution:

```text
                    ┌───────────────┐
                    │ Core Decision │
                    └───────┬───────┘
                            │
                 ┌──────────┼──────────┐
                 ▼          ▼          ▼
              PAPER      SHADOW      LIVE
```

Deployment:

```text
GitHub
  ↓
versioned source
  ↓
Oracle Cloud VM
  ↓
systemd
  ↓
Auto-Exit Bot 24/7
```

---

# 141. Final Security Model

```text
Dedicated Trading Wallet
        +
Limited Capital
        +
No withdrawal functionality
        +
Strong secret protection
        +
No secrets in GitHub
        +
Internal API not publicly exposed
        +
Market allowlist
        +
Order-size limits
        +
Exposure limits
        +
Maximum slippage
        +
Kill switch
        +
Persistent audit logs
        +
Mandatory reconciliation
```

Remember:

```text
"No withdrawal code"
```

is not the same as:

```text
"impossible to withdraw if the private key is compromised"
```

The latter must never be claimed.

---

# 142. Final Build Principle

The most important property of the final Bot is NOT:

```text
"it sells fast"
```

The real goal is:

```text
"it reacts to a valid exit condition quickly,
while never losing track of what has actually happened."
```

In other words:

```text
Fast trigger
+
safe execution
+
persistent state
+
reconciliation
=
reliable Auto-Exit Bot
```

That is the production definition of success for this project.

---

# 143. Current API Verification Requirement

Before writing the actual Polymarket adapter, the AI developer MUST re-check the current official Polymarket documentation/repositories for:

```text
Python SDK package name/version
authentication
wallet/signing requirements
Market WebSocket endpoint
User WebSocket endpoint
book events
price-change events
best-bid/best-ask events
tick-size change events
market-resolution events
current order types
FAK/FOK behavior
current market-order helper semantics
current position API
current open-order API
current trade/fill API
current API rate-limit behavior
current market-version behavior
```

The implementation must pin and document the exact SDK version actually used.

Never copy an old SDK example into production without verifying that it still matches the current official SDK.

---

# 144. Reference Implementation Status

This document is a specification, not the implementation itself.

The coding agent should create the implementation in phases and maintain:

```text
README
CHANGELOG
TEST REPORT
DEPLOYMENT NOTES
CONFIGURATION REFERENCE
```

after each major phase.

Every production-affecting change should include tests.

---

# 145. End State

The finished system should behave like this:

```text
User / Existing Bot
        │
        │ "Monitor this real position
        │  with these exit rules."
        ▼
Auto-Exit Bot
        │
        ├── verifies actual position
        ├── subscribes to token
        ├── maintains live order book
        ├── evaluates SL/TP
        ├── checks current market state
        ├── checks current position
        ├── checks risk limits
        ├── constructs valid tick-aware order
        ├── executes through correct Polymarket interface
        ├── tracks user-side fill events
        ├── reconciles exchange state
        └── resumes safely after failures
```

The Bot must remain a specialized, conservative execution service rather than becoming a second strategy engine.

