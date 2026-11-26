def kernel(event, book, state, orders):
    # ==================================================================
    # BTC-USDT perpetual market maker, third iteration on the brief "shift
    # the quotes with order flow so a trend does not run you over".
    #
    # Diagnostics from trials #0/#1/#2 (all around -0.37 to -0.44, roughly
    # 45k-59k fills over 960 sessions => about 60 fills a session):
    #
    #  * Each 100-lot quote rested all day and filled several times, so the
    #    maker was at the touch nearly continuously and its fills were
    #    selected by the sweeps that print through the touch.  Half a tick of
    #    spread capture against a full tick of post-fill drift is a losing
    #    trade, and more signal only nudged it (0.44 -> 0.38 -> 0.37).
    #
    #  * The single biggest mechanical leak is the stale quote.  We rest a
    #    bid at bid0; when the book steps down a tick our order is now ABOVE
    #    the market and the very next print through it fills us at the old,
    #    too-high price.  Trial #2 only cancelled when the touch had moved
    #    more than TWO spreads away, i.e. it deliberately sat still through
    #    the one-tick pick-off that costs exactly the spread it was trying to
    #    earn.  This version cancels the moment the market trades below a
    #    resting bid (above a resting ask), gated by order flow so ordinary
    #    flicker does not burn the budget.
    #
    #  * The quote is now one-way in its life: place at the touch, hold for
    #    queue position, cancel on pull or on a pick-off, re-place after the
    #    fill.  No chasing the touch upwards -- chasing pays two rows, loses
    #    the queue position and only buys more of the fills that lose money.
    #    A re-centre is allowed only after the market has run three spreads
    #    away and the quote has rested 20 s.
    #
    #  * Inventory: lean full at ~190 lots, force the reducing side past 160,
    #    hard block past 400, last-resort taker flatten past 280 (at most 3 a
    #    session).  The cap is 1000 lots so nothing is ever silently refused.
    #
    #  * Size is cut from 100 to 40 lots.  A resting order sits behind the
    #    displayed size, so its fill probability does not depend on its own
    #    size while its P&L does; with a measured negative edge per fill the
    #    only sane response is a smaller fill.
    #
    # Memory map (64 floats, persists across calls and sessions):
    #   0  EWMA signed aggressive volume (buy - sell), lots
    #   1  ts of the last trade folded into the flow EWMA
    #   2  session id currently tracked
    #   3  order rows spent this session
    #   4  ts of the last requote that cancelled something
    #   5  price of the bid we believe is resting (0 = none)
    #   6  price of the ask we believe is resting (0 = none)
    #   7  EWMA absolute aggressive volume, normaliser for state[0]
    #   8  ts of the first event of this session
    #   9  working id of the resting bid (0 = not learned yet)
    #  10  working id of the resting ask (0 = not learned yet)
    #  11  slow (30 min) EWMA of the mid
    #  12  fast (2 min) EWMA of the mid
    #  13  ts of the last mid EWMA update
    #  14  ts of the last quote evaluation (throttle)
    #  15  pick-off cancels used this session
    #  16  ts of the last pick-off cancel (cooldown before re-quoting)
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
    # TIF_DAY orders die with the old session, so the quote belief, the
    # pick-off counter and the action counter restart.  The position carries.
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
        state[15] = 0.0
        state[16] = 0.0
        state[21] = 0.0
        state[22] = 0.0
        state[23] = 0.0
        state[24] = 0.0
        state[25] = 0.0
        state[26] = 0.0

    # ---- two time-based EWMAs of the mid, the trend guard ---------------
    # Elapsed-time alphas, so a burst of book updates and a sparse run both
    # integrate correctly.  Only book events move the price estimate.
    if event[E_KIND] == EVENT_BOOK:
        if book[B_OK] > 0.5:
            pb = book[B_BID_PX]
            pa = book[B_ASK_PX]
            if pb > 0.0 and pa > pb:
                mid = 0.5 * (pb + pa)
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
    # Signed and absolute volume decay with the same elapsed-time factor, so
    # their ratio is a volume-weighted average aggressor side in [-1, 1] over
    # roughly the last 30 s of tape.
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
    # id above everything seen before belongs to the submission made in the
    # previous call, in row order (bid row first).
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
                    newn = newn + 1
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
        if ts - state[24] < 3000000000:
            return 0
        # It never appeared (refused, or filled on arrival).  Forget it and
        # quote again; the inventory cap is the real backstop.
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
    if pos > 280.0 or pos < -280.0:
        if state[26] < 3.0:
            if ts - state[25] > 600000000000:
                fsz = pos
                if fsz < 0.0:
                    fsz = -fsz
                if fsz > 40.0:
                    fsz = 40.0
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
            bqd = bqd + book[B_BID_QTY + i]
        if i < na:
            aqd = aqd + book[B_ASK_QTY + i]
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

    # 25 bp of fast-vs-slow mid gap is a full unit of trend: a real
    # directional day, not the ordinary wobble of a two-minute average.
    trend = 0.0
    if state[11] > 0.0:
        trend = (state[12] - state[11]) / state[11]
        trend = trend / 0.0025
        if trend > 1.0:
            trend = 1.0
        if trend < -1.0:
            trend = -1.0

    sig = 0.36 * ofi + 0.30 * imb + 0.34 * trend
    if sig > 1.0:
        sig = 1.0
    if sig < -1.0:
        sig = -1.0

    # Inventory lean saturates near 190 lots; the quotes themselves are then
    # the mechanism that flips the position back toward flat.
    inv = pos * 0.008
    if inv > 1.5:
        inv = 1.5
    if inv < -1.5:
        inv = -1.5
    desire = sig - 1.3 * inv

    has_b = 0.0
    has_a = 0.0
    if state[5] > 0.0:
        has_b = 1.0
    if state[6] > 0.0:
        has_a = 1.0

    # Hysteresis: an already-resting side is pulled only when the signal is
    # decisively against it; a side that is out is re-quoted only when the
    # signal has come back to roughly neutral.  Without the gap the quote
    # would flap across a threshold and burn the whole 64-row budget.
    wb = 1.0
    wa = 1.0
    if has_b > 0.5:
        if desire < -0.30:
            wb = 0.0
    else:
        if desire < -0.05:
            wb = 0.0
    if has_a > 0.5:
        if desire > 0.30:
            wa = 0.0
    else:
        if desire > 0.05:
            wa = 0.0

    # Inventory override: past 160 lots the reducing side is quoted even if
    # the signal still points the other way.  Well clear of the 1000-lot cap
    # so a cancel still in flight cannot push the next placement over it.
    if pos > 160.0:
        wb = 0.0
        wa = 1.0
    if pos < -160.0:
        wa = 0.0
        wb = 1.0
    if pos > 400.0:
        wb = 0.0
    if pos < -400.0:
        wa = 0.0

    # ---- pick-off protection --------------------------------------------
    # A resting bid that the market has traded below is now the top of book
    # and will be swept at its old, too-high price.  Cancel it at once --
    # gated by order flow (so ordinary one-tick flicker does not spend the
    # budget) and capped, because at 1 ms latency a cancel is not free.
    sb = 0.0
    sa = 0.0
    if state[15] < 14.0:
        if has_b > 0.5:
            if bid0 < state[5] - 0.000000001:
                if desire < 0.20:
                    if ts - state[16] > 1000000000:
                        sb = 1.0
        if has_a > 0.5:
            if ask0 > state[6] + 0.000000001:
                if desire > -0.20:
                    if ts - state[16] > 1000000000:
                        sa = 1.0

    cb = 0.0
    ca = 0.0
    if has_b > 0.5:
        if wb < 0.5:
            cb = 1.0
        if sb > 0.5:
            cb = 1.0
    if has_a > 0.5:
        if wa < 0.5:
            ca = 1.0
        if sa > 0.5:
            ca = 1.0

    # ---- placement, only at the evaluation throttle ---------------------
    qb = 0.0
    qa = 0.0
    fast = 0.0
    if ts - state[14] >= 1000000000:
        fast = 1.0
        state[14] = float(ts)

    if fast > 0.5:
        # after a pick-off cancel, sit out a few seconds before re-quoting
        # that side: whatever swept us is often still working.
        fresh = 0.0
        if ts - state[16] > 5000000000:
            fresh = 1.0
        if wb > 0.5:
            if has_b < 0.5:
                if fresh > 0.5:
                    qb = bid0
            else:
                # re-centre only after the market has genuinely run away,
                # and only after the quote has earned some queue time.
                if bid0 - state[5] > 3.0 * spread:
                    if ts - state[4] > 20000000000:
                        cb = 1.0
                        qb = bid0
        if wa > 0.5:
            if has_a < 0.5:
                if fresh > 0.5:
                    qa = ask0
            else:
                if state[6] - ask0 > 3.0 * spread:
                    if ts - state[4] > 20000000000:
                        ca = 1.0
                        qa = ask0

    cost = 0.0
    if cb > 0.5:
        cost = cost + 1.0
    if ca > 0.5:
        cost = cost + 1.0
    if qb > 0.0:
        cost = cost + 1.0
    if qa > 0.0:
        cost = cost + 1.0

    # Under budget pressure placements go first: keeping a stale quote alive
    # costs more than losing a little time at the touch.
    if state[3] + cost > budget:
        qb = 0.0
        qa = 0.0
        cost = 0.0
        if cb > 0.5:
            cost = cost + 1.0
        if ca > 0.5:
            cost = cost + 1.0
        if state[3] + cost > budget:
            cb = 0.0
            ca = 0.0
            cost = 0.0

    if cost < 0.5:
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
                    count = count + 1

    if cb > 0.5:
        state[5] = 0.0
        state[9] = 0.0
        if sb > 0.5:
            state[15] = state[15] + 1.0
            state[16] = float(ts)
    if ca > 0.5:
        state[6] = 0.0
        state[10] = 0.0
        if sa > 0.5:
            state[15] = state[15] + 1.0
            state[16] = float(ts)

    pend = 0.0
    side1 = 0.0
    side2 = 0.0

    if qb > 0.0:
        orders[count, O_OP] = OP_BUY
        orders[count, O_KIND] = KIND_LIMIT
        orders[count, O_TIF] = TIF_DAY
        orders[count, O_SIZE] = 40.0
        orders[count, O_LIMIT] = qb
        orders[count, O_ORDER_ID] = 0.0
        count = count + 1
        state[5] = qb
        state[9] = 0.0
        pend = pend + 1.0
        side1 = 1.0

    if qa > 0.0:
        orders[count, O_OP] = OP_SELL
        orders[count, O_KIND] = KIND_LIMIT
        orders[count, O_TIF] = TIF_DAY
        orders[count, O_SIZE] = 40.0
        orders[count, O_LIMIT] = qa
        orders[count, O_ORDER_ID] = 0.0
        count = count + 1
        state[6] = qa
        state[10] = 0.0
        pend = pend + 1.0
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
