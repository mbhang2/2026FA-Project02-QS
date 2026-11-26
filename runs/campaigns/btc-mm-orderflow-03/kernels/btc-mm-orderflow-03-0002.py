def kernel(event, book, state, orders):
    # ==================================================================
    # BTC-USDT perpetual market maker.  One 100-lot quote is rested at the
    # best bid and one at the best ask; both are shifted or pulled by a
    # combined order-flow / book-imbalance / trend signal and leaned away
    # from the inventory we are carrying.  Maker fills pay nothing on this
    # venue, so every row of the 64-row session budget has to buy something:
    # a better price, a better place in the queue, or the avoidance of a
    # fill that is about to be run over.
    #
    # What is different from trial #1 (score -0.3820) and why:
    #
    #  * A trend guard.  Trials #0 and #1 only looked at aggressive flow and
    #    at the book, both of which are local.  On a day that grinds one way
    #    for hours the maker sells the whole way up (buyers lift the resting
    #    ask, the bid never fills) and ends up structurally short, which is a
    #    persistent loss in a market that rose several-fold over the sample.
    #    Here we keep two time-based EWMAs of the mid, 2 min and 30 min, and
    #    put their relative gap into the signal.  A sustained drift pulls the
    #    quote on the side the drift would run over -- the brief's stated
    #    purpose for the flow shift, applied over a horizon the 45 s flow
    #    window cannot see.
    #
    #  * Inventory is bounded harder.  The lean saturates at 250 lots and the
    #    side that would grow the position is switched off outright past 350
    #    lots.  With the cap at 1000 lots and 100-lot quotes the maker can
    #    never approach the cap, so no order is silently refused.
    #
    #  * A last-resort flatten.  If the position is still past 400 lots, we
    #    cross the spread and take 100 lots off, at most four times a session
    #    with a five minute cool-down.  Two basis points is a couple of
    #    hundred ticks of spread capture, so this is deliberately rare: it is
    #    there to cut a position the resting quotes are demonstrably not
    #    reducing, not to trade directionally.
    #
    #  * Queue preservation from trial #1 is kept.  We learn which working id
    #    is the bid and which is the ask, so pulling or moving one side costs
    #    two rows and leaves the other side's place in line untouched.
    #
    # Memory map (64 floats, persists across calls and sessions):
    #   0  EWMA signed aggressive volume (buy - sell), lots
    #   1  ts of the last trade folded into the flow EWMA
    #   2  session id currently tracked
    #   3  order rows spent in this session
    #   4  ts of the last requote that cancelled something
    #   5  price of the bid we believe is resting (0 = none)
    #   6  price of the ask we believe is resting (0 = none)
    #   7  EWMA absolute aggressive volume, the normaliser for state[0]
    #   8  ts of the first event of this session
    #   9  working id of the resting bid (0 = not learned yet)
    #  10  working id of the resting ask (0 = not learned yet)
    #  11  slow (30 min) EWMA of the mid price
    #  12  fast (2 min) EWMA of the mid price
    #  13  ts of the last mid EWMA update
    #  14  ts of the last full quote evaluation (throttle)
    #  20  largest order id we have ever been shown
    #  21  number of submissions in flight (0, 1 or 2)
    #  22  side of the first submission in flight (+1 buy, -1 sell)
    #  23  side of the second submission in flight
    #  24  ts at which those submissions were sent
    #  25  ts of the last flatten order
    #  26  number of flatten orders sent this session
    # ==================================================================
    ts = event[E_TS]
    sess = event[E_SESSION]

    # ---- session bookkeeping -------------------------------------------
    # TIF_DAY orders die with the old session, so the quote belief and the
    # action counter restart.  The position is *not* reset: it is carried.
    # The flow and mid estimates are decaying, so they survive the boundary.
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
        state[25] = 0.0
        state[26] = 0.0

    # ---- two time-based EWMAs of the mid, for the trend guard -----------
    # Updated on book events only: a trade event does not change the book,
    # and the alphas are computed from the elapsed time, so a burst of
    # updates and a sparse run both integrate correctly.
    if event[E_KIND] == EVENT_BOOK:
        if book[B_OK] > 0.5:
            b0 = book[B_BID_PX]
            a0 = book[B_ASK_PX]
            if b0 > 0.0 and a0 > b0:
                mid = 0.5 * (b0 + a0)
                lm = state[13]
                if lm <= 0.0:
                    state[11] = mid
                    state[12] = mid
                    state[13] = float(ts)
                else:
                    dt = (ts - lm) * 0.000000001
                    if dt > 0.0:
                        if dt > 120.0:
                            dt = 120.0
                        af = dt / (120.0 + dt)
                        al = dt / (1800.0 + dt)
                        state[12] = state[12] + (mid - state[12]) * af
                        state[11] = state[11] + (mid - state[11]) * al
                        state[13] = float(ts)

    # ---- aggressive order flow, kept entirely in our own memory ---------
    # Signed and absolute volume decay with the same time-based factor, so
    # their ratio is a volume-weighted average aggressor side in [-1, 1] over
    # roughly the last 30 s of trades.
    if event[E_KIND] == EVENT_TRADE:
        sz = event[E_SIZE]
        if sz > 0.0:
            last = state[1]
            f = 1.0
            if last > 0.0:
                dt = (ts - last) * 0.000000001
                if dt > 0.0:
                    if dt > 600.0:
                        dt = 600.0
                    f = 30.0 / (30.0 + dt)
            state[0] = state[0] * f + event[E_SIDE] * sz
            state[7] = state[7] * f + sz
            state[1] = float(ts)

    # ---- learn which working id belongs to which side -------------------
    # Ids are issued in sequence and the working list is oldest first, so any
    # id larger than everything seen before was issued to the submission made
    # in the previous call, in row order (bid row first, if any).
    w = event[E_N_WORKING]
    newn = 0
    if w > 0:
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
    if w > 0:
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

    # Evaluating the book is cheap but not free on a millisecond book; 1.5 s
    # is fast relative to the 30 s flow window and slow relative to the tape.
    if ts - state[14] < 1500000000:
        return 0
    state[14] = float(ts)

    if book[B_OK] == 0.0:
        return 0
    bid0 = book[B_BID_PX]
    ask0 = book[B_ASK_PX]
    if bid0 <= 0.0 or ask0 <= 0.0 or ask0 <= bid0:
        return 0
    spread = ask0 - bid0

    pos = event[E_POSITION]

    # ---- action budget, paced across the UTC day ------------------------
    elapsed = (ts - state[8]) * 0.000000000000011574
    if elapsed < 0.0:
        elapsed = 0.0
    if elapsed > 1.0:
        elapsed = 1.0
    budget = 6.0 + 54.0 * elapsed

    # ---- last-resort flatten: cut a position the quotes are not cutting --
    if pos > 400.0 or pos < -400.0:
        if state[26] < 4.0:
            if ts - state[25] > 300000000000:
                fsz = pos
                if fsz < 0.0:
                    fsz = -fsz
                if fsz > 100.0:
                    fsz = 100.0
                if state[3] + 1.0 <= budget:
                    if pos > 0.0:
                        orders[0, O_OP] = OP_SELL
                        orders[0, O_LIMIT] = bid0
                    else:
                        orders[0, O_OP] = OP_BUY
                        orders[0, O_LIMIT] = ask0
                    orders[0, O_KIND] = KIND_MARKET
                    orders[0, O_TIF] = TIF_DAY
                    orders[0, O_SIZE] = fsz
                    orders[0, O_ORDER_ID] = 0.0
                    state[25] = float(ts)
                    state[26] = state[26] + 1.0
                    state[3] = state[3] + 1.0
                    return 1

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

    # ---- combine flow, book, trend and inventory ------------------------
    ofi = state[0] / (state[7] + 1.0)
    if ofi > 1.0:
        ofi = 1.0
    if ofi < -1.0:
        ofi = -1.0

    # 15 bp of fast-vs-slow mid gap is a full unit of trend: that is a real
    # directional day, not the ordinary wobble of a two-minute average.
    trend = 0.0
    if state[11] > 0.0:
        trend = (state[12] - state[11]) / state[11]
        trend = trend / 0.0015
        if trend > 1.0:
            trend = 1.0
        if trend < -1.0:
            trend = -1.0

    sig = 0.40 * ofi + 0.30 * imb + 0.30 * trend
    if sig > 1.0:
        sig = 1.0
    if sig < -1.0:
        sig = -1.0

    # Inventory lean saturates at 250 lots; the quotes themselves are then the
    # mechanism that flips the position back toward flat.
    inv = pos * 0.004
    if inv > 1.2:
        inv = 1.2
    if inv < -1.2:
        inv = -1.2
    desire = sig - 1.2 * inv

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
    if bear > 0.35:
        want_bid = 0.0
    if bull > 0.35:
        want_ask = 0.0

    # Inventory override: past 350 lots the reducing side is quoted even if
    # the signal still points the other way.
    if pos > 350.0:
        want_bid = 0.0
        want_ask = 1.0
    if pos < -350.0:
        want_ask = 0.0
        want_bid = 1.0

    # Stay well clear of the inventory cap so a cancel still in flight cannot
    # push the next placement over it.
    if pos > 850.0:
        want_bid = 0.0
    if pos < -850.0:
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
    moved = 0.0

    if state[5] > 0.0:
        if tb <= 0.0:
            cb = 1.0
        else:
            d = tb - state[5]
            if d < 0.0:
                d = -d
            if d > 2.0 * spread:
                cb = 1.0
                qb = tb
                moved = 1.0
    else:
        qb = tb

    if state[6] > 0.0:
        if ta <= 0.0:
            ca = 1.0
        else:
            d = ta - state[6]
            if d < 0.0:
                d = -d
            if d > 2.0 * spread:
                ca = 1.0
                qa = ta
                moved = 1.0
    else:
        qa = ta

    cost = cb + ca
    if qb > 0.0:
        cost += 1.0
    if qa > 0.0:
        cost += 1.0
    if cost < 0.5:
        return 0

    # Moving an already-resting quote throws away its place in line, so it
    # waits out a short rest timer.  A pull does not: the whole point of the
    # pull is that the next print is about to run us over.
    if moved > 0.5:
        if ts - state[4] < 10000000000:
            return 0

    if state[3] + cost > budget:
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
        orders[count, O_ORDER_ID] = 0.0
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
        orders[count, O_ORDER_ID] = 0.0
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
