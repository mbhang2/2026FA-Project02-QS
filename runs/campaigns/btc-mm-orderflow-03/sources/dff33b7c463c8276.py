def kernel(event, book, state, orders):
    # ------------------------------------------------------------------
    # BTC-USDT perpetual market maker that shifts its quotes with order flow.
    #
    # The idea: rest one lot-size limit at the best bid and one at the best ask.
    # Track aggressive buy-minus-sell volume ourselves (every trade event tells
    # us its aggressor and size) and combine it with visible book imbalance.
    # When the combined signal leans bullish we keep the bid at the touch and
    # pull the ask; when it leans bearish we keep the ask and pull the bid.
    # Our own inventory leans the same signal the other way, so a trend that
    # has already filled us stops us adding to it.
    #
    # The run facts cap us at 64 order rows per session, so the scarce resource
    # is not signal quality but quote changes. Every decision to requote is
    # therefore paced against a per-session budget that grows through the day,
    # and we only requote when the desired quote actually differs from what we
    # believe is resting (or when a fill has clearly taken a side away).
    #
    # Memory map (state, 64 floats, persists across calls and sessions):
    #   0  decayed signed aggressive volume (buy - sell), in lots
    #   1  ts (ns) of the last trade consumed
    #   2  session id currently tracked
    #   3  order rows spent in this session
    #   4  ts of our last requote
    #   5  price of the bid we believe is resting (0 = none)
    #   6  price of the ask we believe is resting (0 = none)
    #   7  decayed absolute aggressive volume, the normaliser for state[0]
    #   8  ts of the first event of this session, the pacing denominator
    #  14  ts of the last full quote evaluation (throttle)
    # ------------------------------------------------------------------
    ts = event[E_TS]
    sess = event[E_SESSION]

    # New UTC day: our day orders die with the old one, the action budget and
    # the pacing clock restart. The position is *not* reset -- it is carried.
    if sess != state[2] or state[8] == 0.0:
        state[2] = float(sess)
        state[3] = 0.0
        state[4] = 0.0
        state[5] = 0.0
        state[6] = 0.0
        state[8] = float(ts)

    # ---- order flow, accumulated in our own memory ---------------------
    # Both the signed and the absolute volume decay with the same time-based
    # factor, so their ratio is a flow imbalance in [-1, 1] that needs no
    # ex-post scaling: it is the volume-weighted average aggressor side over
    # roughly the last 15 minutes of trades.
    if event[E_KIND] == EVENT_TRADE:
        sz = event[E_SIZE]
        if sz > 0.0:
            last = state[1]
            f = 1.0
            if last > 0.0 and ts > last:
                dt = (ts - last) * 0.000000001
                if dt > 3600.0:
                    dt = 3600.0
                f = 900.0 / (900.0 + dt)
            state[0] = state[0] * f + event[E_SIDE] * sz
            state[7] = state[7] * f + sz
            state[1] = float(ts)

    if event[E_ACTIONABLE] == 0:
        return 0

    # Evaluating the book is cheap but not free; on a millisecond book this is
    # the difference between a few thousand and a hundred million inspections.
    if ts - state[14] < 5000000000:
        return 0
    state[14] = float(ts)

    # Never requote inside fifteen minutes of the last one: a quote only earns
    # its place in the queue by sitting still.
    if ts - state[4] < 900000000000:
        return 0
    if book[B_OK] == 0.0:
        return 0

    bid0 = book[B_BID_PX]
    ask0 = book[B_ASK_PX]
    if bid0 <= 0.0 or ask0 <= 0.0 or ask0 <= bid0:
        return 0
    spread = ask0 - bid0

    # ---- visible book imbalance ----------------------------------------
    nb = book[B_N_BID]
    na = book[B_N_ASK]

    bq = 0.0
    aq = 0.0
    for i in range(5):
        if i < nb:
            bq += book[B_BID_QTY + i]
        if i < na:
            aq += book[B_ASK_QTY + i]
    tot = bq + aq
    idepth = 0.0
    if tot > 0.0:
        idepth = (bq - aq) / tot

    bq0 = 0.0
    aq0 = 0.0
    if nb > 0.5:
        bq0 = book[B_BID_QTY]
    if na > 0.5:
        aq0 = book[B_ASK_QTY]
    t0 = bq0 + aq0
    itouch = 0.0
    if t0 > 0.0:
        itouch = (bq0 - aq0) / t0

    # The touch is where a fill would actually happen, so weight it higher
    # than the five-level sum, which also counts size we can never reach.
    imb = 0.6 * itouch + 0.4 * idepth

    # ---- combine flow, book and inventory ------------------------------
    ofi = state[0] / (state[7] + 1.0)
    if ofi > 1.0:
        ofi = 1.0
    if ofi < -1.0:
        ofi = -1.0

    sig = 0.65 * ofi + 0.35 * imb

    # Inventory lean: at 400 lots held, the lean is a full unit and cancels
    # any but a very strong signal on the side that would grow the position.
    pos = event[E_POSITION]
    inv = pos * 0.0025
    if inv > 1.0:
        inv = 1.0
    if inv < -1.0:
        inv = -1.0
    desire = sig - 1.2 * inv

    bull = 0.0
    bear = 0.0
    if desire > 0.0:
        bull = desire
    else:
        bear = -desire
    if bull > 1.0:
        bull = 1.0
    if bear > 1.0:
        bear = 1.0

    # A side is quoted at the touch until the signal is strong enough against
    # it. Pulling is deliberately binary: a quote stepped back behind the
    # displayed size at a worse level almost never reaches the front of the
    # queue, so it costs an action and buys nothing over removing it.
    want_bid = 1.0
    want_ask = 1.0
    if bear > 0.35:
        want_bid = 0.0
    if bull > 0.35:
        want_ask = 0.0
    if pos > 400.0:
        want_bid = 0.0
    if pos < -400.0:
        want_ask = 0.0

    db = 0.0
    da = 0.0
    if want_bid > 0.5:
        db = bid0
    if want_ask > 0.5:
        da = ask0

    # ---- action budget, paced across the session -----------------------
    # A hard 64 rows per session would be spent inside the first hour if we
    # requoted whenever the book twitched. Instead the allowance grows with
    # the elapsed fraction of the UTC day, so we hold roughly twelve to
    # fourteen requotes spread over the whole session.
    elapsed = (ts - state[8]) * 0.000000000000011574
    if elapsed < 0.0:
        elapsed = 0.0
    if elapsed > 1.0:
        elapsed = 1.0
    allowed = 8.0 + 48.0 * elapsed
    if state[3] + 8.0 > allowed:
        return 0

    # ---- do we actually need to touch anything? ------------------------
    # A working-order count that disagrees with what we believe is resting
    # means a fill (or a refused order) happened; our side bookkeeping cannot
    # name which id is which, so in that case we rebuild both sides.
    w = event[E_N_WORKING]
    exp = 0.0
    if state[5] > 0.0:
        exp += 1.0
    if state[6] > 0.0:
        exp += 1.0

    need = 0.0
    if w != exp:
        need = 1.0
    if db > 0.0:
        if state[5] <= 0.0:
            need = 1.0
        elif abs(db - state[5]) > 2.0 * spread:
            need = 1.0
    else:
        if state[5] > 0.0:
            need = 1.0
    if da > 0.0:
        if state[6] <= 0.0:
            need = 1.0
        elif abs(da - state[6]) > 2.0 * spread:
            need = 1.0
    else:
        if state[6] > 0.0:
            need = 1.0
    if need < 0.5:
        return 0

    # ---- cancel what is working, then place the two quotes -------------
    # Ids are only ever cancelled from the list the event showed us.
    act = 0
    for i in range(32):
        if i < w and act < 40:
            orders[act, O_OP] = OP_CANCEL
            orders[act, O_ORDER_ID] = event[E_WORKING + i]
            act += 1

    if db > 0.0:
        orders[act, O_OP] = OP_BUY
        orders[act, O_KIND] = KIND_LIMIT
        orders[act, O_TIF] = TIF_DAY
        orders[act, O_SIZE] = 100.0
        orders[act, O_LIMIT] = db
        act += 1
    if da > 0.0:
        orders[act, O_OP] = OP_SELL
        orders[act, O_KIND] = KIND_LIMIT
        orders[act, O_TIF] = TIF_DAY
        orders[act, O_SIZE] = 100.0
        orders[act, O_LIMIT] = da
        act += 1

    if act == 0:
        return 0

    state[3] = state[3] + float(act)
    state[4] = float(ts)
    state[5] = db
    state[6] = da
    return act
