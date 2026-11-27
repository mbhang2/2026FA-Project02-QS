def kernel(event, book, state, orders):
    # ==================================================================
    # BTC-USDT perpetual maker.  Eighth proposal on the brief "shift the
    # quotes with order flow, so a trend does not run you over".
    #
    # Score history on the same 960 real OKX sessions:
    #   #0 -0.438  #1 -0.382  #2 -0.374  #3 -0.383   taker-flatten era
    #   #4 -0.068  touch base + flow/imb/trend lean, no taker  (BEST)
    #   #5 -0.081  ladder one spread behind the touch
    #   #6 -0.215  queue-preserving, rare requotes, sit-out after fills
    #   #7 -0.207  fast 10 s flow + queue-length veto
    #
    # What the seven finished trials say, read as a whole rather than as
    # a parameter table:
    #
    # A. The taker flatten was the whole of -0.38.  Two basis points of
    #    notional is ~200 ticks of captured spread at this tick size, so a
    #    crossed exit can never be paid for by a maker fill.  No market
    #    order appears in this kernel under any condition.
    #
    # B. Depth is not the lever.  #5 moved the whole ladder one spread
    #    back and did the same number of fills for a slightly worse score.
    #    In this fill model a resting order joins BEHIND the displayed
    #    size, so a deeper quote is only reached by a move that has already
    #    travelled there: the fill is the continuation, not the reversion.
    #    The touch is the best place to be whenever we are willing to be
    #    filled at all, and it is where #4 lived.
    #
    # C. Every attempt to add machinery (faster flow #7, queue veto #7,
    #    long rests #6) made it WORSE, and the two trials that put more
    #    time in the market (#6, #7) put more losing fills in the book.
    #    The per-fill edge is a small constant negative: on a one-tick
    #    market the spread does not cover the adverse selection of the
    #    fills you actually get.  Parameter nudges cannot move that; only
    #    *which seconds you are exposed in* can.
    #
    # So this kernel keeps #4 intact -- it is the best tested core and the
    # only one whose fills were not dominated by sweeps -- and makes ONE
    # structural change, aimed squarely at the tail of bad fills rather
    # than at the average:
    #
    #   A GLOBAL STAND-ASIDE GATE ON VOLATILITY BURSTS.
    #   #4 leaned each side back as the 20 s / 30 min |dmid| ratio rose,
    #   but it never actually left the market: even leaning, a touch quote
    #   in a burst is a free option that gets swept.  Here, when the
    #   burst ratio crosses a high threshold, BOTH quotes are cancelled and
    #   NO new quote is placed until the ratio has genuinely fallen back.
    #   Hysteresis (out at volx>0.60, back in only at volx<0.35 after a
    #   2 s dwell) plus a not-too-thin-spread re-entry condition keep it
    #   from flapping and eating the row budget.
    #
    #   The economic claim is narrow and testable: on this fill model the
    #   loss is not spread out evenly across the day, it is concentrated in
    #   the bursts, and the cheapest way to stop paying it is to not be
    #   quoting when one is under way.  Bursts are rare relative to calm
    #   time, so the strategy is still a market maker on most of the tape;
    #   it is simply absent from the seconds that produce the tail.
    #
    # Two smaller, clearly-motivated changes ride along:
    #
    #   * Inventory is clamped harder (the increasing side is switched off
    #     past 120 lots, and the reducing side is pinned to the touch), so
    #     a position cannot be carried into the next funding settlement or
    #     across a trend day.  The 1000-lot cap is never approached, so no
    #     order is ever silently refused.
    #
    #   * The per-side volatility term in the lean is reduced (0.30 not
    #     0.45), because the global gate now handles the regime; the lean
    #     keeps its proven flow/imbalance/trend weights from #4.
    #
    # Kept verbatim from #4 because it was mechanically clean over 960
    # sessions (no refused rows, no invalid cancels): the sequential-id
    # map, fill inference from ids leaving the working list, the in-flight
    # guard, the 1 s evaluation throttle, the row budget paced across the
    # UTC day, and the reactive pick-off cancel as the last backstop.
    #
    # Memory map (64 floats, persists across calls and sessions):
    #   0  EWMA signed aggressive volume (buy - sell), 30 s, lots
    #   1  ts of the last trade folded into the flow EWMA
    #   2  session id currently tracked
    #   3  order rows emitted this session
    #   4  ts of the last requote that cancelled something (rest timer)
    #   5  price of the bid we believe is resting (0 = none)
    #   6  price of the ask we believe is resting (0 = none)
    #   7  EWMA absolute aggressive volume, normaliser for state[0]
    #   8  ts of the first event of this session
    #   9  working id of the resting bid (0 = not learned yet)
    #  10  working id of the resting ask (0 = not learned yet)
    #  11  slow (30 min) EWMA of the mid
    #  12  fast (2 min) EWMA of the mid
    #  13  ts of the last mid / vol / spread update
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
    #  25  long (300 s) EWMA of the quoted spread, for the re-entry gate
    #  26  regime gate state (1 = allowed to quote, 0 = standing aside)
    #  27  ts of the last gate transition (for the dwell)
    # ==================================================================
    ts = event[E_TS]
    sess = event[E_SESSION]

    # ---- session bookkeeping -------------------------------------------
    # TIF_DAY orders die with the old session, so quote beliefs, counters
    # and the pick-off budget restart.  The position and the decaying
    # estimates carry across, because the underlying market never closed.
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
        # Start each session willing to quote; the burst detector will
        # pull us out within a second if the tape is already hot.
        state[26] = 1.0
        state[27] = float(ts)

    # ---- mid EWMAs, two volatility EWMAs, spread EWMA -------------------
    # All alphas are elapsed-time alphas, so a burst of book updates and a
    # sparse run integrate to the same estimate.  Both volatility EWMAs
    # estimate E[|dmid|]; their ratio is ~1 when the tape is stationary and
    # rises when the recent past is more violent than the recent half hour.
    # That ratio is the whole basis of the stand-aside gate below.  The
    # spread EWMA is a slow reference for "is a fresh quote worth placing".
    if event[E_KIND] == EVENT_BOOK:
        if book[B_OK] > 0.5:
            pb0 = book[B_BID_PX]
            pa0 = book[B_ASK_PX]
            if pb0 > 0.0 and pa0 > pb0:
                mid = 0.5 * (pb0 + pa0)
                sp = pa0 - pb0
                lm = state[13]
                if lm <= 0.0:
                    state[11] = mid
                    state[12] = mid
                    state[19] = mid
                    state[25] = sp
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
                        asp = dt / (300.0 + dt)
                        if state[25] <= 0.0:
                            state[25] = sp
                        else:
                            state[25] = state[25] + (sp - state[25]) * asp
                        state[13] = float(ts)

    # ---- aggressive order flow, kept entirely in our own memory ---------
    # Every trade event carries its aggressor and size, so there is no
    # unknown-side branch to write.  Signed and absolute volume decay with
    # the same elapsed-time (30 s) factor, so their ratio is a volume-
    # weighted average aggressor side in [-1, 1] over roughly the last 30 s
    # of tape.  This is #4's window, kept because it was the best tested.
    if event[E_KIND] == EVENT_TRADE:
        sz = event[E_SIZE]
        if sz > 0.0:
            last = state[1]
            f = 2.0
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

    # ---- detect fills: an id we owned that is no longer shown -----------
    # Fills are never reported; the only evidence is a working id vanishing.
    # The belief is dropped and the side becomes eligible to re-quote.
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
    # 64 rows a session is the hard cap and the scarcest thing we own.
    # Pacing it as a smooth ramp means there is always a row left for a
    # protective pull, which is the one thing that must never be skipped.
    elapsed = (ts - state[8]) * 0.000000000000011574
    if elapsed < 0.0:
        elapsed = 0.0
    if elapsed > 1.0:
        elapsed = 1.0
    budget = 10.0 + 50.0 * elapsed

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

    sig = 0.45 * ofi + 0.25 * imb + 0.30 * trend
    if sig > 1.0:
        sig = 1.0
    if sig < -1.0:
        sig = -1.0

    # Inventory lean saturates near 250 lots; the hard rules below bite at
    # 120 so the position oscillates around a small number well inside the
    # 1000-lot cap and no order is ever silently refused.
    inv = pos * 0.006
    if inv > 1.5:
        inv = 1.5
    if inv < -1.5:
        inv = -1.5
    desire = sig - 1.1 * inv

    # Volatility regime: >1 means the last 20 s moved more than the last
    # half hour did.  Bursts are when sweeps happen and when a touch quote
    # is worth nothing.
    volr = 1.0
    if state[18] > 0.000000000001:
        volr = state[17] / state[18]
    volx = (volr - 1.10) / 0.90
    if volx > 1.0:
        volx = 1.0
    if volx < 0.0:
        volx = 0.0

    # ---- the stand-aside gate -------------------------------------------
    # THE one structural change from #4.  When the burst ratio is high,
    # cancel everything and place nothing until it has genuinely fallen
    # back (hysteresis) and the spread is not thinner than usual (a thin
    # spread cannot pay for the adverse selection of the fills it lets in).
    # A 2 s dwell after the last transition stops the gate flapping and
    # burning the row budget on cancel/replace churn.
    sr = 1.0
    if state[25] > 0.0:
        sr = spread / state[25]
    if state[26] > 0.5:
        if volx > 0.60:
            state[26] = 0.0
            state[27] = float(ts)
        else:
            if sr < 0.85:
                state[26] = 0.0
                state[27] = float(ts)
    else:
        if volx < 0.35:
            if sr > 0.98:
                if ts - state[27] > 2000000000:
                    state[26] = 1.0
                    state[27] = float(ts)

    # Directional pressure on each side.  Positive = that side is in
    # trouble, i.e. a fill there is likely to be followed by the price
    # going the wrong way.  Both sides inherit a reduced volatility term,
    # because the global gate now carries the regime.
    pb = 0.0
    if desire < 0.0:
        pb = -desire
    pa = 0.0
    if desire > 0.0:
        pa = desire
    pb = pb + 0.30 * volx
    pa = pa + 0.30 * volx

    # Inventory override.  Past 120 lots the side that would GROW the
    # position is removed outright and the side that would SHRINK it is
    # pinned to the touch, so an exit always exists and a position is not
    # carried into the next funding settlement.
    if pos > 120.0:
        pb = 2.0
        pa = 0.0
    if pos < -120.0:
        pa = 2.0
        pb = 0.0

    has_b = 0.0
    if state[5] > 0.0:
        has_b = 1.0
    has_a = 0.0
    if state[6] > 0.0:
        has_a = 1.0

    # ---- where each side wants to be ------------------------------------
    # Base is the touch, because #5 proved a ladder that starts one spread
    # back only earns a worse fill.  Hysteresis: a side already stepped
    # back only returns to the touch once the pressure has genuinely
    # fallen, and a side at the touch only steps back once it has risen.
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
        thr_b = 0.40
    thr_a = 0.20
    if cur_a_back < 0.5:
        thr_a = 0.40

    wb = bid0
    if pb >= 0.62:
        wb = 0.0
    else:
        if pb >= thr_b:
            wb = bid0 - spread
            if wb <= 0.0:
                wb = 0.0

    wa = ask0
    if pa >= 0.62:
        wa = 0.0
    else:
        if pa >= thr_a:
            wa = ask0 + spread

    # The gate overrides everything: standing aside means pulling both
    # sides and placing nothing, so the existing cancel machinery below
    # does the work and no fresh row is emitted.
    if state[26] < 0.5:
        wb = 0.0
        wa = 0.0

    # ---- pick-off protection --------------------------------------------
    # A quote the market has travelled THROUGH is stale and will be swept
    # at a price nobody else is on.  Rare now that we stand aside in a
    # burst; kept as the backstop for a jump with no warning.
    sb = 0.0
    sa = 0.0
    if state[15] < 12.0:
        if ts - state[16] > 1000000000:
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
                    if ts - state[16] > 5000000000:
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
                    if ts - state[16] > 5000000000:
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
    # its P&L does; #2/#3 proved size is not the lever.
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
