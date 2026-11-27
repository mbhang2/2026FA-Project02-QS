def kernel(event, book, state, orders):
    # ==================================================================
    # BTC-USDT perpetual market maker: one unit of size resting at the best
    # bid and the best ask, with the quotes shifted and pulled by order flow
    # (kept in our own memory) and visible book imbalance, and leaned away
    # from whatever inventory we are carrying.
    #
    # What changed from trial #0 (score -0.4378), and why:
    #
    #  * The order-flow EWMA had a 900 s time constant.  Measured against a
    #    real 64-row session budget that is functionally a constant: the
    #    combined signal almost never crossed trial #0's 0.35 pull threshold,
    #    so in practice it quoted both sides at the touch all day and ate
    #    nothing but adverse selection.  A 45 s constant lets an actual burst
    #    of one-sided aggression move the signal.
    #
    #  * Inventory lean is about three times stronger (full lean at ~330 lots
    #    instead of ~400 at 1.2x).  A market maker that carries a position for
    #    hours pays funding at 00:00/08:00/16:00 UTC and eats the drift; the
    #    position has to be flipped back toward flat by the quotes themselves.
    #
    #  * We track which working id is our bid and which is our ask.  A requote
    #    that only moves one side then costs 2 rows instead of 4, and, far more
    #    importantly, it does not throw away the untouched side's queue
    #    position.  Under the queue-pessimistic fill model a resting quote
    #    earns its fill by sitting still and moving forward as trades print at
    #    its price; resetting it to the back of the queue every time the other
    #    side needs to move is the most expensive thing a market maker can do.
    #
    #  * A pull (the signal says do not quote this side) is executed at once.
    #    Only a same-side price move has to wait out a short rest timer.
    #
    # Memory map (64 floats, persists across calls and sessions):
    #   0  EWMA signed aggressive volume (buy - sell), lots
    #   1  ts of the last trade folded into the EWMA
    #   2  session id currently tracked
    #   3  order rows spent in this session
    #   4  ts of the last requote that cancelled something
    #   5  price of the bid we believe is resting (0 = none)
    #   6  price of the ask we believe is resting (0 = none)
    #   7  EWMA absolute aggressive volume, the normaliser for state[0]
    #   8  ts of the first event of this session
    #   9  working id of the resting bid (0 = not learned yet)
    #  10  working id of the resting ask (0 = not learned yet)
    #  14  ts of the last full quote evaluation (throttle)
    #  20  largest order id we have ever been shown
    #  21  number of submissions in flight (0, 1 or 2)
    #  22  side of the first submission in flight (+1 buy, -1 sell)
    #  23  side of the second submission in flight
    #  24  ts at which those submissions were sent
    # ==================================================================
    ts = event[E_TS]
    sess = event[E_SESSION]

    # ---- session bookkeeping -------------------------------------------
    # TIF_DAY orders die with the old session, so the whole quote belief is
    # thrown away at the boundary.  The position is *not* reset; it is carried.
    if sess != state[2] or state[8] == 0.0:
        state[2] = float(sess)
        state[3] = 0.0
        state[4] = 0.0
        state[5] = 0.0
        state[6] = 0.0
        state[8] = float(ts)
        state[9] = 0.0
        state[10] = 0.0
        state[14] = 0.0
        state[21] = 0.0
        state[22] = 0.0
        state[23] = 0.0
        state[24] = 0.0

    # ---- aggressive order flow, kept entirely in our own memory ---------
    # Signed and absolute volume decay with the same time-based factor, so
    # their ratio is a volume-weighted average aggressor side in [-1, 1] over
    # roughly the last 45 s of trades, with no ex-post scaling needed.
    if event[E_KIND] == EVENT_TRADE:
        sz = event[E_SIZE]
        if sz > 0.0:
            last = state[1]
            f = 1.0
            if last > 0.0:
                dt = (ts - last) * 0.000000001
                if dt > 0.0:
                    if dt > 900.0:
                        dt = 900.0
                    f = 45.0 / (45.0 + dt)
            state[0] = state[0] * f + event[E_SIDE] * sz
            state[7] = state[7] * f + sz
            state[1] = float(ts)

    # ---- learn which working id belongs to which side -------------------
    # Ids are issued in sequence and the working list is oldest first, so any
    # id larger than everything we have seen before was issued to the
    # submission we made in the previous call, in row order (bid row first).
    w = event[E_N_WORKING]
    newn = 0
    for i in range(32):
        if i < w:
            oid = event[E_WORKING + i]
            if oid > state[20]:
                state[20] = float(oid)
                if newn == 0:
                    if state[22] > 0.5:
                        state[9] = float(oid)
                    elif state[22] < -0.5:
                        state[10] = float(oid)
                else:
                    if state[23] > 0.5:
                        state[9] = float(oid)
                    elif state[23] < -0.5:
                        state[10] = float(oid)
                newn += 1
    if newn > 0:
        state[21] = 0.0
        state[22] = 0.0
        state[23] = 0.0

    # ---- detect fills: an id we owned that is no longer shown -----------
    b_present = 0.0
    a_present = 0.0
    for i in range(32):
        if i < w:
            oid = event[E_WORKING + i]
            if state[9] > 0.0:
                if oid == state[9]:
                    b_present = 1.0
            if state[10] > 0.0:
                if oid == state[10]:
                    a_present = 1.0
    if state[21] < 0.5:
        if state[9] > 0.0:
            if b_present < 0.5:
                state[9] = 0.0
                state[5] = 0.0
        else:
            if state[5] > 0.0:
                state[5] = 0.0
        if state[10] > 0.0:
            if a_present < 0.5:
                state[10] = 0.0
                state[6] = 0.0
        else:
            if state[6] > 0.0:
                state[6] = 0.0

    if event[E_ACTIONABLE] == 0:
        return 0

    # ---- wait for a submission to appear before making another one ------
    if state[21] > 0.5:
        if ts - state[24] < 10000000000:
            return 0
        # It never appeared (should not happen at 1 ms latency).  Forget it
        # and quote again; the inventory cap protects us from doubling up.
        if state[22] > 0.5:
            state[5] = 0.0
            state[9] = 0.0
        if state[22] < -0.5:
            state[6] = 0.0
            state[10] = 0.0
        if state[23] > 0.5:
            state[5] = 0.0
            state[9] = 0.0
        if state[23] < -0.5:
            state[6] = 0.0
            state[10] = 0.0
        state[21] = 0.0
        state[22] = 0.0
        state[23] = 0.0

    # Book inspection is cheap but not free on a millisecond book; two seconds
    # is fast relative to the 45 s flow window and slow relative to the tape.
    if ts - state[14] < 2000000000:
        return 0
    state[14] = float(ts)

    if book[B_OK] == 0.0:
        return 0
    bid0 = book[B_BID_PX]
    ask0 = book[B_ASK_PX]
    if bid0 <= 0.0 or ask0 <= 0.0 or ask0 <= bid0:
        return 0
    spread = ask0 - bid0

    # ---- visible book imbalance -----------------------------------------
    nb = book[B_N_BID]
    na = book[B_N_ASK]

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

    bqd = 0.0
    aqd = 0.0
    for i in range(5):
        if i < nb:
            bqd += book[B_BID_QTY + i]
        if i < na:
            aqd += book[B_ASK_QTY + i]
    td = bqd + aqd
    idepth = 0.0
    if td > 0.0:
        idepth = (bqd - aqd) / td

    # The touch is where a fill would actually happen, so weight it above the
    # five-level sum, which also counts size we can never reach from here.
    imb = 0.55 * itouch + 0.45 * idepth

    # ---- combine flow, book and inventory -------------------------------
    ofi = state[0] / (state[7] + 1.0)
    if ofi > 1.0:
        ofi = 1.0
    if ofi < -1.0:
        ofi = -1.0

    sig = 0.55 * ofi + 0.45 * imb
    if sig > 1.0:
        sig = 1.0
    if sig < -1.0:
        sig = -1.0

    pos = event[E_POSITION]
    inv = pos * 0.003
    if inv > 1.5:
        inv = 1.5
    if inv < -1.5:
        inv = -1.5
    desire = sig - 1.1 * inv

    bull = 0.0
    bear = 0.0
    if desire > 0.0:
        bull = desire
    else:
        bear = -desire
    if bull > 1.5:
        bull = 1.5
    if bear > 1.5:
        bear = 1.5

    # A side is quoted at the touch until the signal is decisively against it.
    # Pulling is binary on purpose: a quote stepped back behind the displayed
    # size at a worse level almost never reaches the front of the queue, so it
    # costs an action and buys nothing over simply removing it.
    want_bid = 1.0
    want_ask = 1.0
    if bear > 0.25:
        want_bid = 0.0
    if bull > 0.25:
        want_ask = 0.0

    # Inventory override: past 300 lots the reducing side is quoted even if the
    # signal still points the other way.  This is the part of the brief that
    # leans the quotes away from the position held.
    if pos > 300.0:
        want_bid = 0.0
        want_ask = 1.0
    if pos < -300.0:
        want_ask = 0.0
        want_bid = 1.0

    # Stay well clear of the inventory cap so a cancel still in flight cannot
    # push the next placement over it.
    if pos > 700.0:
        want_bid = 0.0
    if pos < -700.0:
        want_ask = 0.0

    tb = 0.0
    ta = 0.0
    if want_bid > 0.5:
        tb = bid0
    if want_ask > 0.5:
        ta = ask0

    # ---- what has to change ---------------------------------------------
    cb = 0.0
    ca = 0.0
    qb = 0.0
    qa = 0.0
    pulled = 0.0
    moved = 0.0

    if state[5] > 0.0:
        if tb <= 0.0:
            cb = 1.0
            pulled = 1.0
        else:
            if abs(tb - state[5]) > 2.0 * spread:
                cb = 1.0
                qb = tb
                moved = 1.0
    else:
        qb = tb

    if state[6] > 0.0:
        if ta <= 0.0:
            ca = 1.0
            pulled = 1.0
        else:
            if abs(ta - state[6]) > 2.0 * spread:
                ca = 1.0
                qa = ta
                moved = 1.0
    else:
        qa = ta

    nplace = 0.0
    if qb > 0.0:
        nplace += 1.0
    if qa > 0.0:
        nplace += 1.0
    cost = cb + ca + nplace
    if cost < 0.5:
        return 0

    # A move of an already-resting quote waits out a short rest timer; a pull
    # does not, because the whole point of the pull is that the next print is
    # about to run us over.
    if moved > 0.5:
        if ts - state[4] < 60000000000:
            return 0

    # ---- action budget, paced across the UTC day ------------------------
    elapsed = (ts - state[8]) * 0.000000000000011574
    if elapsed < 0.0:
        elapsed = 0.0
    if elapsed > 1.0:
        elapsed = 1.0
    if state[3] + cost > 6.0 + 54.0 * elapsed:
        return 0

    # ---- emit: cancel by id, only ids this event actually showed --------
    count = 0
    if cb > 0.5 or ca > 0.5:
        for i in range(32):
            if i < w:
                oid = event[E_WORKING + i]
                hit = 0.0
                if cb > 0.5:
                    if state[9] > 0.0:
                        if oid == state[9]:
                            hit = 1.0
                if ca > 0.5:
                    if state[10] > 0.0:
                        if oid == state[10]:
                            hit = 1.0
                if hit > 0.5:
                    orders[count, O_OP] = OP_CANCEL
                    orders[count, O_ORDER_ID] = oid
                    count += 1

    if cb > 0.5:
        state[5] = 0.0
        state[9] = 0.0
    if ca > 0.5:
        state[6] = 0.0
        state[10] = 0.0

    pend = 0.0
    side1 = 0.0
    side2 = 0.0

    if qb > 0.0:
        orders[count, O_OP] = OP_BUY
        orders[count, O_KIND] = KIND_LIMIT
        orders[count, O_TIF] = TIF_DAY
        orders[count, O_SIZE] = 100.0
        orders[count, O_LIMIT] = qb
        count += 1
        state[5] = qb
        state[9] = 0.0
        pend += 1.0
        side1 = 1.0

    if qa > 0.0:
        orders[count, O_OP] = OP_SELL
        orders[count, O_KIND] = KIND_LIMIT
        orders[count, O_TIF] = TIF_DAY
        orders[count, O_SIZE] = 100.0
        orders[count, O_LIMIT] = qa
        count += 1
        state[6] = qa
        state[10] = 0.0
        pend += 1.0
        if side1 == 0.0:
            side1 = -1.0
        else:
            side2 = -1.0

    if count == 0:
        return 0

    if pend > 0.0:
        state[21] = pend
        state[22] = side1
        state[23] = side2
        state[24] = float(ts)

    state[3] = state[3] + float(count)
    if cb > 0.5 or ca > 0.5:
        state[4] = float(ts)
    return count
