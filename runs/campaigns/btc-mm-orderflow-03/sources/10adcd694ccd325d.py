def kernel(event, book, state, orders):
    # ==================================================================
    # BTC-USDT perpetual market maker.  Seventh proposal on the brief
    # "shift the quotes with order flow, so a trend does not run you over".
    #
    # Score history, all on the same 960 real OKX sessions:
    #   #0 -0.438  #1 -0.382  #2 -0.374  #3 -0.383   taker-flatten era
    #   #4 -0.068  touch base + lean-back, no taker order        (BEST)
    #   #5 -0.081  ladder starting one spread behind the touch
    #   #6 -0.215  queue-preserving, rare requotes, sit-out after fill
    #
    # Three things those seven trials establish, and this kernel is built
    # on them rather than on another parameter sweep:
    #
    # A. THE TAKER FLATTEN WAS THE WHOLE OF THE -0.38.  Going from "touch
    #    quoting + market-order flatten" to "touch quoting + lean-back,
    #    never cross" bought 0.31 of score.  Two basis points of notional
    #    is ~200 ticks of captured spread at this tick size, so a crossed
    #    exit can never be paid for by a maker fill.  There is no market
    #    order anywhere in this kernel, under any condition.
    #
    # B. DEEPER IS WORSE, NOT BETTER.  #5 moved the whole ladder one
    #    spread back and lost 0.013 more while doing the same number of
    #    fills.  In this fill model a resting order joins BEHIND the
    #    displayed size at its price, so a deeper quote is only reached by
    #    a move that has already travelled there -- the fill is the
    #    continuation, not the reversion.  Depth buys entry edge and pays
    #    for it with a worse fill.  The touch is the best place to be when
    #    we are willing to be filled at all.
    #
    # C. MORE TIME IN THE MARKET IS WORSE.  #6 held its quotes far longer
    #    (1.6-spread displacement, 180 s minimum rest) and its fill count
    #    rose 39.5k -> 51k while its score fell 0.147.  The fills we get by
    #    simply being present are the negative-edge ones.  So the scarce
    #    resource is not order rows and it is not queue position: it is
    #    exposure while the tape is uninformative, and it should be cut.
    #
    # What that leaves is a kernel whose only job is to decide, per side,
    # whether a fill right now would be a fair one.  Three changes from
    # #4, each aimed at fill SELECTION rather than at price:
    #
    # 1. A FASTER, SHARPER FLOW TERM.  The adverse move that eats a maker
    #    fill is a seconds-scale object; #4's 30 s EWMA leans on flow that
    #    has already been absorbed.  The horizon here is 10 s and the lean
    #    is sharper (step-back 0.28, removal 0.60, instead of 0.40/0.62),
    #    so a side steps back sooner and disappears sooner when the tape
    #    is one-way.  Book imbalance and the fast/slow mid gap confirm it.
    #
    # 2. A QUEUE-LENGTH VETO.  This is the new mechanism and it falls
    #    straight out of the fill rule.  If the displayed size at the
    #    touch on our side is many times its own recent average, then
    #    joining behind it can only be filled by whatever finally eats
    #    that whole wall -- a sweep, i.e. the worst fill available.  So a
    #    NEW quote is refused on a side whose touch size exceeds four
    #    times its 60 s average.  Ordinary and thin queues are quoted
    #    exactly as before; only the wall is refused.  The filter costs
    #    nothing when it is not binding and removes a class of fills that
    #    no amount of leaning can make fair.
    #
    # 3. INVENTORY IS TAKEN SERIOUSLY, STILL NEVER CROSSED.  A 40-lot
    #    fill is one unit of signal, so the lean saturates near 140 lots
    #    (1.5 * pos * 0.007) and the hard rules bite at 160 / 300 lots:
    #    past 160 the side that would GROW the position is removed
    #    outright, past 300 the side that would SHRINK it is pinned to the
    #    touch so an exit always exists.  The 1000-lot cap is never
    #    approached, so no order is ever silently refused.
    #
    # Kept from #4 because they were mechanically clean over 960 sessions
    # (no refused rows, no invalid cancels): the sequential-id map, fill
    # inference from ids leaving the working list, the in-flight guard,
    # the 1 s evaluation throttle, the row budget paced across the UTC
    # day, and the reactive pick-off cancel as the last backstop.
    #
    # Memory map (64 floats, persists across calls and sessions):
    #   0  fast (10 s) EWMA signed aggressive volume, lots
    #   1  ts the flow EWMA was last advanced
    #   2  session id currently tracked
    #   3  order rows emitted this session
    #   4  ts of the last requote that cancelled something (rest timer)
    #   5  price of the bid we believe is resting (0 = none)
    #   6  price of the ask we believe is resting (0 = none)
    #   7  fast EWMA absolute aggressive volume
    #   8  ts of the first event of this session
    #   9  working id of the resting bid (0 = not learned yet)
    #  10  working id of the resting ask (0 = not learned yet)
    #  11  slow (30 min) EWMA of the mid
    #  12  fast (2 min) EWMA of the mid
    #  13  ts of the last mid / volatility update
    #  14  ts of the last throttled quote evaluation
    #  15  pick-off cancels used this session
    #  16  ts of the last pick-off cancel
    #  17  short (20 s) EWMA of |change in mid|
    #  18  long (30 min) EWMA of |change in mid|
    #  19  last mid, for the volatility estimate
    #  20  largest order id we have ever been shown
    #  21  number of submissions in flight (0, 1 or 2)
    #  22  side of the first submission in flight (+1 buy, -1 sell)
    #  23  side of the second submission in flight
    #  24  ts at which those submissions were sent
    #  25  EWMA of the displayed size at the best bid
    #  26  EWMA of the displayed size at the best ask
    #  27  ts of the last touch-size update
    # ==================================================================
    ts = event[E_TS]
    sess = event[E_SESSION]

    # ---- session bookkeeping -------------------------------------------
    # TIF_DAY orders die with the old session, so quote beliefs, counters
    # and the pick-off budget restart.  The position and the decaying
    # estimates carry across, because the market never closed.
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

    # ---- mid EWMAs, two volatility EWMAs, two touch-size EWMAs ----------
    # Elapsed-time alphas throughout, so a burst of book updates and a
    # sparse run integrate to the same estimate.  The volatility EWMAs
    # both estimate E[|dmid|]; their ratio is ~1 when the tape is
    # stationary and rises when the recent past is more violent than the
    # recent half hour.  The touch-size EWMAs are the reference against
    # which a "wall" at the best price is judged.
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
                            # seed on the first observable move; a stale
                            # zero would make the ratio meaningless
                            state[17] = dm
                            state[18] = dm
                        else:
                            av = dt / (20.0 + dt)
                            aw = dt / (1800.0 + dt)
                            state[17] = state[17] + (dm - state[17]) * av
                            state[18] = state[18] + (dm - state[18]) * aw
                        state[13] = float(ts)
                lt = state[27]
                if lt <= 0.0:
                    state[25] = book[B_BID_QTY]
                    state[26] = book[B_ASK_QTY]
                    state[27] = float(ts)
                else:
                    dt2 = (ts - lt) * 0.000000001
                    if dt2 > 0.0:
                        if dt2 > 60.0:
                            dt2 = 60.0
                        aq2 = dt2 / (60.0 + dt2)
                        state[25] = state[25] + (book[B_BID_QTY] - state[25]) * aq2
                        state[26] = state[26] + (book[B_ASK_QTY] - state[26]) * aq2
                        state[27] = float(ts)

    # ---- aggressive order flow, kept entirely in our own memory ---------
    # Every trade event carries its aggressor and size, so there is no
    # unknown-side branch to write.  Signed and absolute volume decay with
    # the same elapsed-time factor, so their ratio is a volume-weighted
    # average aggressor side in [-1, 1] over roughly the last 10 s.
    if event[E_KIND] == EVENT_TRADE:
        sz = event[E_SIZE]
        if sz > 0.0:
            last = state[1]
            f = 2.0
            if last > 0.0:
                dt = (ts - last) * 0.000000001
                if dt > 0.0:
                    if dt > 300.0:
                        dt = 300.0
                    f = 10.0 / (10.0 + dt)
            state[0] = state[0] * f + event[E_SIDE] * sz
            state[7] = state[7] * f + sz
            state[1] = float(ts)

    # ---- learn which working id belongs to which side -------------------
    # Ids are issued in sequence and the working list is oldest first, so
    # any id above everything we have seen belongs to the submission made
    # in the previous call, in row order (bid row first when both sent).
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
    # Fills are never reported; the only evidence is a working id we
    # believed in vanishing.  The belief is dropped and the side becomes
    # eligible to re-quote, which is how a partial exit gets replaced.
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
        # it never appeared (refused, or filled before we saw the id):
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

    # ---- action budget, paced across the UTC day ------------------------
    # 62 of the 64 rows are spent by this ramp; the last two are held back
    # so the day can never end with no way to address an order.  Every row
    # bought something: a better price, a queue slot, or the avoidance of
    # a fill about to be run over.
    elapsed = (ts - state[8]) * 0.000000000000011574
    if elapsed < 0.0:
        elapsed = 0.0
    if elapsed > 1.0:
        elapsed = 1.0
    budget = 8.0 + 54.0 * elapsed

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

    # ---- combine flow, book, trend into one directional pressure --------
    ofi = state[0] / (state[7] + 1.0)
    if ofi > 1.0:
        ofi = 1.0
    if ofi < -1.0:
        ofi = -1.0

    # 20 bp of fast-vs-slow mid gap is a full unit of trend: a real
    # directional day, not the ordinary wobble of a two-minute average.
    trend = 0.0
    if state[11] > 0.0:
        trend = (state[12] - state[11]) / state[11]
        trend = trend / 0.0020
        if trend > 1.0:
            trend = 1.0
        if trend < -1.0:
            trend = -1.0

    sig = 0.55 * ofi + 0.25 * imb + 0.20 * trend
    if sig > 1.0:
        sig = 1.0
    if sig < -1.0:
        sig = -1.0

    # Inventory lean saturates near 140 lots, well inside the 1000-lot cap
    # so no order is ever silently refused.
    inv = pos * 0.007
    if inv > 1.5:
        inv = 1.5
    if inv < -1.5:
        inv = -1.5
    desire = sig - 1.5 * inv

    # Volatility regime: >1 means the last 20 s moved more than the last
    # half hour did.  Both sides step back when it is hot; a touch quote is
    # only worth having when the mid is calm enough not to pick us off.
    volr = 1.0
    if state[18] > 0.000000000001:
        volr = state[17] / state[18]
    volx = (volr - 1.10) / 0.90
    if volx > 1.0:
        volx = 1.0
    if volx < 0.0:
        volx = 0.0

    # Directional pressure on each side.  Positive = that side is in
    # trouble, i.e. a fill there is likely to be followed by the price
    # going the wrong way.  Both sides inherit the volatility term.
    pb = 0.0
    if desire < 0.0:
        pb = -desire
    pa = 0.0
    if desire > 0.0:
        pa = desire
    pb = pb + 0.40 * volx
    pa = pa + 0.40 * volx

    # Inventory override.  Past 160 lots the side that would GROW the
    # position is removed outright; past 300 the side that would SHRINK it
    # is pinned to the touch so an exit always exists.
    if pos > 160.0:
        pb = 2.0
        pa = 0.0
        if pos > 300.0:
            pb = 2.0
            pa = 0.0
    if pos < -160.0:
        pa = 2.0
        pb = 0.0

    has_b = 0.0
    has_a = 0.0
    if state[5] > 0.0:
        has_b = 1.0
    if state[6] > 0.0:
        has_a = 1.0

    # ---- where each side wants to be ------------------------------------
    # Base is the touch (#5 established that starting one spread back only
    # earns a worse fill).  Hysteresis: a side already stepped back only
    # returns to the touch once the pressure has genuinely fallen, and a
    # side already at the touch only steps back once it has genuinely
    # risen.  Without the gap the quote would flap across the threshold
    # and eat the whole row budget.
    cur_b_back = 0.0
    if state[5] > 0.0:
        if state[5] < bid0 - 0.5 * spread:
            cur_b_back = 1.0
    cur_a_back = 0.0
    if state[6] > 0.0:
        if state[6] > ask0 + 0.5 * spread:
            cur_a_back = 1.0

    thr_b = 0.14
    if cur_b_back < 0.5:
        thr_b = 0.28
    thr_a = 0.14
    if cur_a_back < 0.5:
        thr_a = 0.28

    wb = bid0
    if pb >= 0.60:
        wb = 0.0
    else:
        if pb >= thr_b:
            wb = bid0 - spread
            if wb <= 0.0:
                wb = 0.0

    wa = ask0
    if pa >= 0.60:
        wa = 0.0
    else:
        if pa >= thr_a:
            wa = ask0 + spread

    # ---- queue-length veto ----------------------------------------------
    # A new quote joins behind the displayed size at its price, so a wall
    # at the touch can only ever be filled by whatever finally eats the
    # whole wall -- a sweep.  Refuse to join a side whose touch size is
    # more than four times its own 60 s average.  Existing quotes are left
    # alone; this only gates fresh placements.
    if state[25] > 0.0:
        if bq0 > 4.0 * state[25]:
            if has_b < 0.5:
                wb = 0.0
    if state[26] > 0.0:
        if aq0 > 4.0 * state[26]:
            if has_a < 0.5:
                wa = 0.0

    # ---- pick-off protection --------------------------------------------
    # A quote the market has travelled THROUGH is stale and will be swept
    # at a price nobody else is on.  This should be rare now that a side
    # steps back when the tape turns; it is kept as the backstop for a
    # jump that arrives with no warning, and it is capped because at 1 ms
    # of latency a cancel is usually a row spent on the past.
    sb = 0.0
    sa = 0.0
    if state[15] < 14.0:
        if ts - state[16] > 500000000:
            if has_b > 0.5:
                if bid0 < state[5] - 0.000000001:
                    sb = 1.0
            if has_a > 0.5:
                if ask0 > state[6] + 0.000000001:
                    sa = 1.0

    # ---- decide cancels and placements ----------------------------------
    cb = 0.0
    ca = 0.0
    qb = 0.0
    qa = 0.0

    # pulls are immediate: the next print is the one we are avoiding
    if has_b > 0.5:
        if wb <= 0.0:
            cb = 1.0
    if has_a > 0.5:
        if wa <= 0.0:
            ca = 1.0
    if sb > 0.5:
        cb = 1.0
    if sa > 0.5:
        ca = 1.0

    # moves and fresh placements only at the 1 s throttle: a millisecond
    # book would otherwise burn the whole day inside one minute
    if ts - state[14] >= 1000000000:
        state[14] = float(ts)
        if cb < 0.5:
            if has_b > 0.5:
                if wb > 0.0:
                    d = wb - state[5]
                    if d < 0.0:
                        d = -d
                    if d > 0.6 * spread:
                        # moving a resting quote throws away its place in
                        # line, so it must have earned some queue time
                        if ts - state[4] > 20000000000:
                            cb = 1.0
                            qb = wb
            else:
                if wb > 0.0:
                    # after a pick-off, sit out a little: whatever swept us
                    # is often still working
                    if ts - state[16] > 4000000000:
                        qb = wb
        if ca < 0.5:
            if has_a > 0.5:
                if wa > 0.0:
                    d = wa - state[6]
                    if d < 0.0:
                        d = -d
                    if d > 0.6 * spread:
                        if ts - state[4] > 20000000000:
                            ca = 1.0
                            qa = wa
            else:
                if wa > 0.0:
                    if ts - state[16] > 4000000000:
                        qa = wa

    # a cancel we cannot address is not a cancel
    if cb > 0.5:
        if state[9] <= 0.0:
            cb = 0.0
            qb = 0.0
    if ca > 0.5:
        if state[10] <= 0.0:
            ca = 0.0
            qa = 0.0

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

    # Under budget pressure placements go first: keeping a stale quote
    # alive costs more than losing a little time at the touch.
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

    # 40 lots is 0.004 BTC.  A resting order sits behind displayed size, so
    # its fill probability per lot does not depend on its own size while
    # its P&L does; trials #2/#3 proved size is not the lever.
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
