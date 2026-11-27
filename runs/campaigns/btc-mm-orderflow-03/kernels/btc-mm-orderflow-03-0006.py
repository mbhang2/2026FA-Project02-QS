def kernel(event, book, state, orders):
    # ==================================================================
    # BTC-USDT perpetual maker.  Sixth try on the brief "shift the quotes
    # with order flow, so a trend does not run you over".
    #
    # Where the six finished trials actually landed:
    #   #0..#3  -0.44 .. -0.37   two-sided touch quoting, taker flatten
    #   #4       -0.068          touch base + lean-back + no taker flatten
    #   #5       -0.081          ladder starting one spread back
    #
    # Two facts carry all the information those trials contain.  First,
    # going from touch-plus-taker-flatten to touch-plus-lean-back bought
    # 0.31 of score; going one rung deeper (#5) bought nothing.  So the
    # profitable thing is not depth, it is *which* fills you take, and the
    # touch is where the benign ones live -- sitting one spread back only
    # ever gets you hit by the moves that genuinely travel.  Second,
    # cutting size 100 -> 40 lots moved the score by 0.01, so the loss is
    # per unit of notional filled, i.e. a fill-selection problem, not a
    # risk-sizing problem.
    #
    # Both remaining ideas here are about fill SELECTION, not price:
    #
    # 1. QUEUE POSITION IS A REAL ASSET AND #4 THREW IT AWAY.  A resting
    #    order joins BEHIND the displayed size at its price and only moves
    #    forward as trades print there.  #4 requoted whenever the target
    #    moved 0.6 of a spread, which on a one-tick-spread market is every
    #    few seconds; it was permanently at the back of the queue and so
    #    filled only when a sweep took the whole level out -- exactly the
    #    fills that lose.  Here a voluntary requote needs BOTH a 1.6-spread
    #    displacement AND a long minimum rest, so the quote sits and lets
    #    the queue in front of it drain.  A cheap bid left behind by a
    #    rising market is not a problem to fix; it is a good price.
    #
    # 2. REACTIVE CANCELS ARE ALMOST USELESS, SO STOP PAYING FOR THEM.
    #    The book we are shown is the newest row strictly before this
    #    millisecond and a cancel lands a further 1 ms later.  If the bid
    #    has already ticked below our quote, the seller that did it has
    #    already gone through us; cancelling is a row spent on the past.
    #    #4 burned 10-12 rows a session on that.  Here the pick-off
    #    backstop fires only when the market is 1.5 spreads THROUGH the
    #    quote and is capped at 8 a session.  The rows saved go to the one
    #    thing that must never be skipped: re-quoting a side that has just
    #    been filled, so we are not out of the market.
    #
    # The lean itself stays close to #4 because it is the only part that
    # provably earned anything: signed aggressive flow over 15 s and 120 s,
    # visible-book imbalance, fast-vs-slow mid trend, and a 20 s / 30 min
    # volatility-ratio burst term.  A side that is being leaned against
    # steps back one spread, then is removed.  The flow window is shorter
    # than #4's 30 s because a stale lean is worse than a noisy one -- the
    # message we are avoiding arrives within seconds.
    #
    # Inventory: the side that would SHRINK the position is pinned to the
    # touch past 120 lots, the side that would GROW it is removed past 240.
    # No taker order anywhere: 2 bp of notional is ~200 ticks of spread at
    # this tick size and can never be clawed back by one fill.
    #
    # Memory map (64 floats, persists across calls and sessions):
    #   0  fast (15 s) EWMA signed aggressive volume
    #   1  ts the flow EWMAs were last advanced
    #   2  session id tracked
    #   3  order rows emitted this session
    #   4  ts of the last requote (rest timer)
    #   5  price of the bid we believe is resting (0 = none)
    #   6  price of the ask we believe is resting (0 = none)
    #   7  fast EWMA absolute aggressive volume
    #   8  ts of the first event of this session
    #   9  working id of the resting bid (0 = not learned)
    #  10  working id of the resting ask (0 = not learned)
    #  11  slow (30 min) EWMA of the mid
    #  12  fast (2 min) EWMA of the mid
    #  13  ts of the last mid / volatility update
    #  14  ts of the last quote evaluation
    #  15  pick-off cancels used this session
    #  16  ts of the last pick-off cancel
    #  17  short (20 s) EWMA of |change in mid|
    #  18  long (30 min) EWMA of |change in mid|
    #  19  last mid
    #  20  largest order id ever shown
    #  21  submissions in flight (0, 1 or 2)
    #  22  side of the first submission in flight (+1 buy, -1 sell)
    #  23  side of the second submission in flight
    #  24  ts at which those submissions were sent
    #  25  slow (120 s) EWMA signed aggressive volume
    #  26  slow EWMA absolute aggressive volume
    #  27  ts of the last INFERRED fill
    # ==================================================================
    ts = event[E_TS]
    sess = event[E_SESSION]

    # ---- session reset --------------------------------------------------
    # TIF_DAY orders die with the session, so quote beliefs, the id map and
    # the counters restart.  The position and the decaying estimates carry
    # over, because the underlying market never closed.
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
        state[27] = 0.0

    # ---- aggressive order flow, entirely in our own memory --------------
    # Every trade tells us its aggressor and size, so there is no unknown
    # branch to write here.  Both a fast (15 s) and a slow (120 s) elapsed-
    # time EWMA of signed and absolute volume are kept: their ratio is a
    # volume-weighted average aggressor side in [-1, 1] over that horizon.
    if event[E_KIND] == EVENT_TRADE:
        sz = event[E_SIZE]
        if sz > 0.0:
            dt = 0.0
            if state[1] > 0.0:
                dt = (ts - state[1]) * 0.000000001
                if dt < 0.0:
                    dt = 0.0
                if dt > 300.0:
                    dt = 300.0
            ff = 15.0 / (15.0 + dt)
            fs = 120.0 / (120.0 + dt)
            sgn = event[E_SIDE]
            state[0] = state[0] * ff + sgn * sz
            state[7] = state[7] * ff + sz
            state[25] = state[25] * fs + sgn * sz
            state[26] = state[26] * fs + sz
            state[1] = float(ts)

    # ---- mid trend and the two volatility EWMAs -------------------------
    # Both volatility EWMAs estimate E[|dmid|]; the ratio is ~1 when the
    # tape is stationary and rises when the recent past is more violent
    # than the recent half hour.  That is the regime term: a burst is when
    # sweeps happen and when a touch quote is worth nothing.
    if event[E_KIND] == EVENT_BOOK:
        if book[B_OK] > 0.5:
            pb0 = book[B_BID_PX]
            pa0 = book[B_ASK_PX]
            if pb0 > 0.0 and pa0 > pb0:
                mid = 0.5 * (pb0 + pa0)
                lm = state[13]
                if lm <= 0.0:
                    state[11] = mid
                    state[12] = mid
                    state[19] = mid
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
                        dm = mid - state[19]
                        if dm < 0.0:
                            dm = -dm
                        state[19] = mid
                        if state[18] <= 0.0:
                            state[17] = dm
                            state[18] = dm
                        else:
                            av = dt / (20.0 + dt)
                            aw = dt / (1800.0 + dt)
                            state[17] = state[17] + (dm - state[17]) * av
                            state[18] = state[18] + (dm - state[18]) * aw
                        state[13] = float(ts)

    # ---- learn which working id is our bid and which is our ask ---------
    # Ids are issued in sequence and the working list is oldest first, so
    # any id above everything seen before belongs to the submission made in
    # the previous call, in row order (bid row first when both were sent).
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

    # ---- infer fills: an id we own that is no longer shown --------------
    # Fills are never reported.  The only evidence is a working id we
    # believed in vanishing, so the belief is dropped, the side becomes
    # eligible to re-quote, and the fill is timestamped so that the side
    # sits out a couple of seconds while whatever swept it finishes.
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
                state[27] = float(ts)
        else:
            if state[5] > 0.0:
                state[5] = 0.0
        if state[10] > 0.0:
            if a_present < 0.5:
                state[10] = 0.0
                state[6] = 0.0
                state[27] = float(ts)
        else:
            if state[6] > 0.0:
                state[6] = 0.0

    if event[E_ACTIONABLE] == 0:
        return 0

    # ---- wait for a submission to appear before making another one ------
    if state[21] > 0.5:
        if ts - state[24] < 3000000000:
            return 0
        # it never showed up (refused, or filled before we saw the id):
        # forget it and let the ordinary logic re-quote
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

    # ---- action budget, paced smoothly across the UTC day ---------------
    # 64 rows a session is the hard cap and it is the scarcest thing we
    # own.  Pacing it as a smooth ramp rather than a front-loaded one
    # means there is always a row left for re-quoting a side that has just
    # filled, which is the one placement that must never be skipped.
    elapsed = (ts - state[8]) * 0.000000000000011574
    if elapsed < 0.0:
        elapsed = 0.0
    if elapsed > 1.0:
        elapsed = 1.0
    budget = 3.0 + 58.0 * elapsed

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

    # The touch is where a fill would actually happen, so it outweighs the
    # five-level sum, which counts size we can never reach from here.
    imb = 0.55 * itouch + 0.45 * idepth

    # ---- one directional signal -----------------------------------------
    ofi_f = state[0] / (state[7] + 1.0)
    if ofi_f > 1.0:
        ofi_f = 1.0
    if ofi_f < -1.0:
        ofi_f = -1.0

    ofi_s = state[25] / (state[26] + 1.0)
    if ofi_s > 1.0:
        ofi_s = 1.0
    if ofi_s < -1.0:
        ofi_s = -1.0

    trend = 0.0
    if state[11] > 0.0:
        trend = (state[12] - state[11]) / state[11]
        trend = trend / 0.0020
        if trend > 1.0:
            trend = 1.0
        if trend < -1.0:
            trend = -1.0

    sig = 0.35 * ofi_f + 0.10 * ofi_s + 0.25 * imb + 0.30 * trend
    if sig > 1.0:
        sig = 1.0
    if sig < -1.0:
        sig = -1.0

    # Inventory lean: saturates near 145 lots, well inside the 1000-lot
    # cap so no order is ever silently refused.
    inv = pos * 0.007
    if inv > 1.5:
        inv = 1.5
    if inv < -1.5:
        inv = -1.5
    desire = sig - 1.2 * inv

    # Volatility regime: >1 means the last 20 s moved more than the last
    # half hour did.  Both sides step back when it is hot.
    volr = 1.0
    if state[18] > 0.000000000001:
        volr = state[17] / state[18]
    volx = (volr - 1.10) / 0.90
    if volx > 1.0:
        volx = 1.0
    if volx < 0.0:
        volx = 0.0

    # Pressure on each side.  Positive = that side is in trouble, i.e. a
    # fill there is likely to be followed by the price going the wrong way.
    pb = 0.0
    if desire < 0.0:
        pb = -desire
    pa = 0.0
    if desire > 0.0:
        pa = desire
    pb = pb + 0.40 * volx
    pa = pa + 0.40 * volx

    # Inventory override.  Past 120 lots the reducing side is pinned to the
    # touch so an exit is always available; past 240 the increasing side is
    # switched off outright.  The position oscillates around a small number
    # and never approaches the cap.
    if pos > 120.0:
        pa = 0.0
        if pos > 240.0:
            pb = 2.0
    if pos < -120.0:
        pb = 0.0
        if pos < -240.0:
            pa = 2.0

    has_b = 0.0
    if state[5] > 0.0:
        has_b = 1.0
    has_a = 0.0
    if state[6] > 0.0:
        has_a = 1.0

    # ---- where each side wants to be ------------------------------------
    # Base is the touch, because #5 showed a ladder that starts one spread
    # back only ever takes the toxic fills.  Hysteresis: a side already
    # stepped back only returns once the pressure has genuinely fallen, and
    # a side at the touch only steps back once it has genuinely risen.
    cur_b_back = 0.0
    if state[5] > 0.0:
        if state[5] < bid0 - 0.5 * spread:
            cur_b_back = 1.0
    cur_a_back = 0.0
    if state[6] > 0.0:
        if state[6] > ask0 + 0.5 * spread:
            cur_a_back = 1.0

    thr_b = 0.20
    if cur_b_back < 0.5:
        thr_b = 0.42
    thr_a = 0.20
    if cur_a_back < 0.5:
        thr_a = 0.42

    wb = bid0
    if pb >= 0.68:
        wb = 0.0
    else:
        if pb >= thr_b:
            wb = bid0 - spread
            if wb <= 0.0:
                wb = 0.0

    wa = ask0
    if pa >= 0.68:
        wa = 0.0
    else:
        if pa >= thr_a:
            wa = ask0 + spread

    # ---- decide cancels and placements ----------------------------------
    cb = 0.0
    ca = 0.0
    qb = 0.0
    qa = 0.0
    move_b = 0.0
    move_a = 0.0

    # A pull is immediate: it is the next print we are avoiding and it does
    # not need a reason beyond the target having gone to zero.
    if has_b > 0.5:
        if wb <= 0.0:
            cb = 1.0
    if has_a > 0.5:
        if wa <= 0.0:
            ca = 1.0

    rest = ts - state[4]
    since_fill = ts - state[27]

    # ---- the bid --------------------------------------------------------
    if cb < 0.5:
        if has_b < 0.5:
            # Nothing resting: re-quote.  This is the placement that must
            # always be affordable, because being out of the market after a
            # fill costs every fill that would have come next.
            if wb > 0.0:
                if since_fill > 2000000000:
                    qb = wb
        else:
            if wb > 0.0:
                if wb - state[5] > 1.6 * spread:
                    # the market has run away upward.  Chasing costs the
                    # queue position we have, so it needs a long rest and a
                    # large displacement before it is worth the two rows.
                    if rest > 180000000000:
                        cb = 1.0
                        qb = wb
                        move_b = 1.0
                else:
                    if state[5] - wb > 1.5 * spread:
                        # the market has run away downward, so the quote is
                        # now above the touch.  This is the pick-off guard;
                        # it is capped because 1 ms of latency means it
                        # usually arrives too late to be worth much.
                        if rest > 30000000000:
                            if state[15] < 8.0:
                                cb = 1.0
                                qb = wb
                                move_b = 1.0
                                state[15] = state[15] + 1.0
                                state[16] = float(ts)

    # ---- the ask, symmetric ---------------------------------------------
    if ca < 0.5:
        if has_a < 0.5:
            if wa > 0.0:
                if since_fill > 2000000000:
                    qa = wa
        else:
            if wa > 0.0:
                if state[6] - wa > 1.6 * spread:
                    if rest > 180000000000:
                        ca = 1.0
                        qa = wa
                        move_a = 1.0
                else:
                    if wa - state[6] > 1.5 * spread:
                        if rest > 30000000000:
                            if state[15] < 8.0:
                                ca = 1.0
                                qa = wa
                                move_a = 1.0
                                state[15] = state[15] + 1.0
                                state[16] = float(ts)

    # a cancel we cannot address is not a cancel
    if cb > 0.5:
        if state[9] <= 0.0:
            cb = 0.0
            qb = 0.0
            move_b = 0.0
    if ca > 0.5:
        if state[10] <= 0.0:
            ca = 0.0
            qa = 0.0
            move_a = 0.0

    # ---- budget ---------------------------------------------------------
    cost = 0.0
    if cb > 0.5:
        cost = cost + 1.0
    if ca > 0.5:
        cost = cost + 1.0
    if qb > 0.0:
        cost = cost + 1.0
    if qa > 0.0:
        cost = cost + 1.0
    if cost < 0.5:
        return 0

    # Under budget pressure the ORDER OF SACRIFICE is: voluntary price
    # moves first (they only recover queue position we chose to give up),
    # then fresh placements, and only then a protective pull, because a
    # stale quote we cannot afford to cancel is the one thing that really
    # costs money.
    if state[3] + cost > budget:
        if move_b > 0.5:
            cb = 0.0
            qb = 0.0
            move_b = 0.0
        if move_a > 0.5:
            ca = 0.0
            qa = 0.0
            move_a = 0.0
        cost = 0.0
        if cb > 0.5:
            cost = cost + 1.0
        if ca > 0.5:
            cost = cost + 1.0
        if qb > 0.0:
            cost = cost + 1.0
        if qa > 0.0:
            cost = cost + 1.0
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

    # ---- emit: cancel only ids this event actually showed ---------------
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
    if ca > 0.5:
        state[6] = 0.0
        state[10] = 0.0

    pend = 0.0
    side1 = 0.0
    side2 = 0.0

    # 40 lots is 0.004 BTC.  A resting order sits behind the displayed size,
    # so its fill probability per lot does not depend on its own size while
    # its P&L does; trials #2/#3 proved size is not the lever, so it stays
    # small and the inventory stays cheap.
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
