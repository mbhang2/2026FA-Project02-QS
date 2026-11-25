# The kernel interface

A kernel is one Python function that the harness compiles to native code and calls on every
event. It is the whole strategy. It sees one event, the visible book and its own memory, and it
returns the orders it wants. It cannot see anything later than the event it is handed.

```python
def kernel(event, book, state, orders):
    ...
    return number_of_order_rows_used
```

The file must contain exactly that one function and nothing else: no imports, no other
functions, no module-level code, no docstrings. Explain your reasoning in `#` comments.

## When it is called

- **On every trade.** `event[E_KIND] == EVENT_TRADE`.
- **On every book update.** The venue's timestamps are milliseconds, so a book update becomes
  visible at the start of the next millisecond. All updates in one millisecond produce one
  event, showing the last of them, and it comes before a trade at that same instant.
  `event[E_KIND] == EVENT_BOOK`. Book events run from just after a session's first trade
  through its last.
- **During warmup** as well, so `state` can accumulate. There `event[E_ACTIONABLE] == 0` and any
  orders returned are ignored.

## `event`: integers, read-only

| name | meaning |
|---|---|
| `E_TS` | event time, nanoseconds since the epoch |
| `E_SESSION` | session id (one UTC day) |
| `E_KIND` | `EVENT_TRADE` or `EVENT_BOOK` |
| `E_SIZE` | trade size in lots; 0 on a book event |
| `E_SIDE` | trade aggressor: `+1` buyer, `-1` seller; 0 on a book event |
| `E_POSITION` | your signed position in lots |
| `E_ACTIONABLE` | 1 when orders count, 0 during warmup |
| `E_N_WORKING` | how many of your orders are still working, at most 32 shown |
| `E_WORKING` | first of up to 32 working order ids, oldest first: `event[E_WORKING + i]` |

## `book`: floats, read-only

| name | meaning |
|---|---|
| `B_PRICE` | the last trade price |
| `B_OK` | 1.0 when a two-sided book no older than one second is visible, else 0.0 and every level below is 0 |
| `B_N_BID`, `B_N_ASK` | how many levels are filled, at most 5 each |
| `B_BID_PX + i`, `B_BID_QTY + i` | bid level `i`, best first; size in lots |
| `B_ASK_PX + i`, `B_ASK_QTY + i` | ask level `i`, best first; size in lots |

The visible book is the latest row strictly before the current millisecond.

## `state`: your memory

64 floats, zero when the run starts, kept across every event and session. Nothing else
persists between calls, so anything you need to remember goes here.

## `orders`: what you return

A zeroed table of 64 rows of 6 floats. Fill rows from 0 upward and return how many you used.

| column | meaning |
|---|---|
| `O_OP` | `OP_BUY`, `OP_SELL` or `OP_CANCEL` |
| `O_KIND` | `KIND_MARKET` or `KIND_LIMIT` |
| `O_TIF` | `TIF_DAY` (cancelled at the session close) or `TIF_GTC` (kept) |
| `O_SIZE` | whole lots, at least 1 |
| `O_LIMIT` | limit price, above zero; ignored for market orders |
| `O_ORDER_ID` | for `OP_CANCEL`: one of the ids in `event[E_WORKING + i]` |

A row with an invalid value, a cancel of an id you were not shown, or a count outside 0 to 64
stops the run. That is a trial, it counts, and it scores nothing.

## How orders behave

- **Ids** are given in sequence from 1. You are not told an order's id when you send it; it
  appears among the working ids on later events. An order refused by the inventory cap still
  uses its id.
- **Latency** is 1 ms. An order arrives 1 ms after the event that sent it, and is eligible
  from the next event after that. A cancel takes effect after 1 ms; until then the order can
  still fill, and its id stays in the working list.
- **Fills happen only on trades.** You are not called with fills; infer them from
  `E_POSITION` and from ids leaving the working list.
- **A resting limit order** joins behind the displayed size at its price. It moves forward
  only when trades print at its price, and fills when trade volume exceeds what is ahead of
  it or a trade prints through its price.
- **A market order, or a limit that crosses**, takes the visible levels when it arrives. An
  order larger than the five visible levels is refused. No order fills against a one-sided book.
- **Inventory cap.** An order is dropped if your position, plus every working order on the
  same side, plus this order would exceed the cap in the run facts below.

## Costs

Makers pay 0 bp and takers 2 bp of notional. One lot is 0.0001 BTC. Funding is charged on
positions held at settlement times. Your own orders do not move the book.

## The language

- Arithmetic, comparisons, `and`, `or`, `not`, `if`/`else`, conditional expressions.
- `for i in range(n)` with `n` a number literal up to 64, nested at most two deep. No `while`.
- Calls only to `abs`, `min`, `max`, `int`, `float` and `range`.
- Writes only to `state[...]`, `orders[...]` and your own local names. `event` and `book` are
  read-only, and the argument names and constants above cannot be reassigned.
- No strings, lists, tuples, attributes, imports, lambdas or other functions.
- Assign a local name before reading it on every path.
- Keep numbers ordinary. The harness checks your kernel by also running its source as plain
  Python, and a kernel whose integers overflow 64 bits, or that converts NaN to an integer,
  behaves differently there. That invalidates the trial.

## Constants

`E_TS`, `E_SESSION`, `E_SIZE`, `E_POSITION`, `E_SIDE`, `E_ACTIONABLE`, `E_N_WORKING`,
`E_WORKING`, `E_KIND`, `EVENT_TRADE`, `EVENT_BOOK`, `B_PRICE`, `B_OK`, `B_N_BID`, `B_N_ASK`,
`B_BID_PX`, `B_BID_QTY`, `B_ASK_PX`, `B_ASK_QTY`, `O_OP`, `O_KIND`, `O_TIF`, `O_SIZE`,
`O_LIMIT`, `O_ORDER_ID`, `OP_BUY`, `OP_SELL`, `OP_CANCEL`, `KIND_MARKET`, `KIND_LIMIT`,
`TIF_DAY`, `TIF_GTC`.
