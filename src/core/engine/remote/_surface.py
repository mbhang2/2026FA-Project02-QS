"""The context surface, classified. GENERATED -- do not edit.

Written by `scripts/gen_remote_surface.py` from
`src/core/engine/context.py`, the same source `docs/design/context-api.md`
is generated from, so the boundary cannot drift from the interface it
carries. Edit the context, then regenerate.

`scalars` are pushed as-is. `collections` are pushed through the named
encoder. `pulls` are methods the child asks for and the parent answers.
`actions` are intents the child returns and the parent replays.
"""

from __future__ import annotations

SETUPCONTEXT_SCALARS = ('dataset_name',)
SETUPCONTEXT_COLLECTIONS = ()
SETUPCONTEXT_PULLS = ('subscriptions', 'ticks', 'train_bars')
SETUPCONTEXT_ACTIONS = ('subscribe',)

BASECONTEXT_SCALARS = ('actionable', 'average_entry_price', 'equity', 'last_price', 'position', 'realized_pnl', 'session', 'session_end', 'session_start', 'ts', 'unrealized_pnl', 'working_order_ids')
BASECONTEXT_COLLECTIONS = (('bars', 'closed_bars_row'), ('closed', 'bar_row'), ('fills', 'fill_row'), ('now', 'datetime_row'), ('working_orders', 'order_row'))
BASECONTEXT_PULLS = ('closed_ids', 'find_orders', 'history', 'options', 'sub_id', 'ticks')
BASECONTEXT_ACTIONS = ('buy', 'cancel', 'cancel_all', 'close', 'modify', 'sell')

BARCONTEXT_SCALARS = ()
BARCONTEXT_COLLECTIONS = ()
BARCONTEXT_PULLS = ()
BARCONTEXT_ACTIONS = ()

FILLCONTEXT_SCALARS = ()
FILLCONTEXT_COLLECTIONS = (('fill', 'fill_row'),)
FILLCONTEXT_PULLS = ()
FILLCONTEXT_ACTIONS = ()

LIFECYCLECONTEXT_SCALARS = ()
LIFECYCLECONTEXT_COLLECTIONS = (('event', 'lifecycle_row'),)
LIFECYCLECONTEXT_PULLS = ()
LIFECYCLECONTEXT_ACTIONS = ()

TICKCONTEXT_SCALARS = ()
TICKCONTEXT_COLLECTIONS = (('tick', 'tick_row'),)
TICKCONTEXT_PULLS = ()
TICKCONTEXT_ACTIONS = ()

SURFACE = {
    'SetupContext': ('dataset_name', 'subscribe', 'subscriptions', 'ticks', 'train_bars'),
    'BaseContext': ('actionable', 'average_entry_price', 'bars', 'buy', 'cancel', 'cancel_all', 'close', 'closed', 'closed_ids', 'equity', 'fills', 'find_orders', 'history', 'last_price', 'modify', 'now', 'options', 'position', 'realized_pnl', 'sell', 'session', 'session_end', 'session_start', 'sub_id', 'ticks', 'ts', 'unrealized_pnl', 'working_order_ids', 'working_orders'),
    'BarContext': (),
    'FillContext': ('fill',),
    'LifecycleContext': ('event',),
    'TickContext': ('tick',),
}

ENCODERS = {'Bar': 'bar_row', 'TickEvent': 'tick_row', 'FillEvent': 'fill_row', 'LifecycleEvent': 'lifecycle_row', 'OrderView': 'order_row', 'Subscription': 'subscription_row', 'datetime': 'datetime_row', 'dt.datetime': 'datetime_row', '_ClosedBars': 'closed_bars_row', 'Tick': 'raw_tick_row', 'OptionQuote': 'option_quote_row'}
