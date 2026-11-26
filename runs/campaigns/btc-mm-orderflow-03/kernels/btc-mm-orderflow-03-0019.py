def kernel(event, book, state, orders):
    # ==================================================================
    # BTC-USDT perpetual maker.  Nineteenth proposal on the brief
    # "shift the quotes with order flow, so a trend does not run you
    # over".
    #
    # Score history on the same 960 real OKX sessions:
    #   #0 -0.438  #1 -0.382  #2 -0.374  #3 -0.383   taker-flatten era
    #   #4 -0.068  touch quoting + flow/imb/trend lean, no taker
    #   #5 -0.081  ladder one spread behind the touch
    #   #6 -0.215  queue-preserving, rare requotes
    #   #7 -0.207  fast 10 s flow + queue-length veto
    #   #8 -0.085  #4 + global stand-aside on volatility bursts
    #   #9 -0.049  #4 + one tick INSIDE the touch at spread >= 3 ticks
    #   #10 -0.062 #9 + a "thin queue" gate on touch quotes
    #   #11 -0.051 #9 + the inside-quote false pick-off corrected
    #   #12 -0.322 #11 + mid-crossing exits + a size lean
    #   #13 -0.088 #11 + a four-fold tighter inventory band
    #   #14 -0.088 #11 + the one-spread step-back rung DELETED
    #   #15 -0.062 #11 + a four-rung spread-based ladder
    #   #16 -0.050 #11 with the momentum term deleted
    #   #17 -0.051 #11 + the two-tick spread covered by one side at mid
    #   #18 -0.051 #16 + a 240 s order-flow window
    #
    # WHAT THE EIGHTEEN SAY, AS ONE RESULT:
    #
    # * Fees are settled: no market order appears below under any
    #   condition; every fill is a resting maker fill at 0 bp.
    # * Everything that quoted LESS lost (#5, #8, #10, #13, #14).
    # * Everything that moved the PRICE of a quote off the touch or
    #   changed how often it is replaced moved nothing or lost (#6, #7,
    #   #15, #17, #18).  The 240 s flow window of #18 and the 30 s one it
    #   replaced are the same score to three decimals: the order-flow
    #   term, as a PRICE shifter, is inert here.
    # * The only change that ever gained is #9's inside quote (+0.019),
    #   and it is a queue-position change, not a price-shift change.
    #
    # WHY THE FLOW TERM IS INERT, AND WHAT TO DO ABOUT IT.
    # Look at the arithmetic the ladder actually performs.  With typical
    # values ofi ~ 0.2, imb ~ 0.3, trend ~ 0.1 the composite signal sits
    # near 0.15, while the step-back threshold is 0.40 for a side at the
    # touch and withdrawal needs 0.62.  So on an ordinary minute the
    # shift never fires at all, and #16 deleting a third of the signal
    # moved 0.0003 -- exactly what that arithmetic predicts.  The one
    # place the flow view does bite is the INVENTORY term (1.1 * inv
    # dwarfs sig once |pos| passes ~150 lots), which is why this maker
    # has really been an inventory-flattener with a flow decoration.
    #
    # THE CHANGE IN #19: express the flow view as SIZE, not as distance.
    #
    #     bid size = 40 * (1 + 0.8 * sig),  ask size = 40 * (1 - 0.8*sig)
    #     clamped to [8, 72] whole lots.
    #
    # When aggressive buying dominates the tape the bid is the side whose
    # fill is likely to be followed by a rise, so it is placed large; the
    # ask is the side facing the run-over, so it is placed small.  Three
    # reasons this is the right place to put the signal:
    #
    #  - It costs no rows.  The size is chosen at placement time, so the
    #    64-row session budget -- the binding resource, per #15 and #17
    #    losing precisely because they spent rows on worse quotes -- is
    #    untouched.  A PRICE shift always costs a cancel plus a replace;
    #    a size tilt costs nothing extra.
    #  - It does not give up a fill.  The record's strongest single
    #    regularity is that suppressing quotes loses money (#5, #8, #10,
    #    #14).  A price shift removes the side from the touch; a size
    #    tilt keeps both sides present and simply weights them.
    #  - It is the smooth version of the same statement, so it keeps
    #    acting where the ladder's hard thresholds cut off at 0.40.
    #
    # Everything else is the best mechanically clean base (#11/#16): the
    # 30 s flow window that carries the record, the three-state ladder
    # with hysteresis, #9's single inside-placement rule (spread >= 3
    # ticks => bid at bid0 + step, ask at ask0 - step), the loose 200-lot
    # inventory override, the volatility stretch that never gates, the
    # sequential-id map, fill inference from ids leaving the working
    # list, the in-flight guard, the 1 s evaluation throttle, the 20 s
    # minimum rest, the day-paced row budget, the pick-off backstop with
    # its inside-quote tolerance, and the 12-pick-off session budget.
    #
    # One mechanical repair carried over from #11: #18's ask-side
    # pick-off was written as `sa = sa` and never cancelled anything.
    # It is restored to `ca = 1.0`.
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
    #  13  ts of the last mid / vol update
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
    #  25  step: smallest two-sided spread ever seen (a valid tick
    #      multiple); 0 until the first valid book arrives
    #  28  1 if the resting bid was placed INSIDE the touch, else 0
    #  29  1 if the resting ask was placed INSIDE the touch, else 0
    # ==================================================================
    ts = event[E_TS]
    sess = event[E_SESSION]

    # ---- session bookkeeping -------------------------------------------
    # TIF_DAY orders die with the old session, so quote beliefs, counters,
    # the inside/touch flags and the pick-off budget restart.  Position,
    # the decaying estimates and the step estimate carry across: the
    # market never closed and the tick never changed.
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
        state[28] = 0.0
        state[29] = 0.0

    # ---- mid EWMAs, two volatility EWMAs, and the step estimate --------
    # Elapsed-time alphas, so a burst of book updates and a sparse run
    # integrate to the same estimate.  Both volatility EWMAs estimate
    # E[|dmid|]; their ratio is ~1 on a stationary tape.  The spread is
    # tracked only as a running minimum: that is the tick estimate the
    # inside placement needs, and every observed spread is an integer
    # multiple of it, so bid0 + step is always a legal price.
    if event[E_KIND] == EVENT_BOOK:
        if book[B_OK] > 0.5:
            pb0 = book[B_BID_PX]
            pa0 = book[B_ASK_PX]
            if pb0 > 0.0 and pa0 > pb0:
                sp = pa0 - pb0
                if state[25] <= 0.0:
                    state[25] = sp
                else:
                    if sp < state[25]:
                        state[25] = sp
                mid = 0.5 * (pb0 + pa0)
                lm = state[13]
                if lm <= 0.0:
                    state[11] = mid
                    state[12] = mid
                    state[19] = mid
                    state[13] = float(ts)
                else:
                    dtb = (ts - lm) * 0.000000001
                    if dtb > 0.0:
                        if dtb > 120.0:
                            dtb = 120.0
                        af = dtb / (120.0 + dtb)
                        al = dtb / (1800.0 + dtb)
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
                            av = dtb / (20.0 + dtb)
                            aw = dtb / (1800.0 + dtb)
                            state[17] = state[17] + (dm - state[17]) * av
                            state[18] = state[18] + (dm - state[18]) * aw
                        state[13] = float(ts)

    # ---- aggressive order flow, kept entirely in our own memory ---------
    # Every trade event carries its aggressor and size, so there is no
    # unknown-side branch to write.  Signed and absolute volume decay with
    # the same elapsed-time factor, so their ratio is a volume-weighted
    # average aggressor side in [-1, 1] over roughly the last 30 s of
    # tape.  Thirty seconds is the window that carries the record; #18
    # showed a four-minute one is the same score, so the horizon is not
    # what has been limiting this signal -- see the header.
    if event[E_KIND] == EVENT_TRADE:
        sz = event[E_SIZE]
        if sz > 0.0:
            last = state[1]
            f = 2.0
            if last > 0.0:
                dtt = (ts - last) * 0.000000001
                if dtt > 0.0:
                    if dtt > 600.0:
                        dtt = 600.0
                    f = 30.0 / (30.0 + dtt)
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
    # Fills are never reported; the only evidence is a working id we
    # believed in vanishing.  The belief -- price and inside/touch flag --
    # is dropped together, so the side becomes eligible to re-quote.
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
                state[28] = 0.0
        else:
            if state[5] > 0.0:
                state[5] = 0.0
                state[28] = 0.0
        if state[10] > 0.0:
            if a_present < 0.5:
                state[10] = 0.0
                state[6] = 0.0
                state[29] = 0.0
        else:
            if state[6] > 0.0:
                state[6] = 0.0
                state[29] = 0.0

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
            state[28] = 0.0
        if state[22] < -0.5:
            state[6] = 0.0
            state[10] = 0.0
            state[29] = 0.0
        if state[23] > 0.5:
            state[5] = 0.0
            state[9] = 0.0
            state[28] = 0.0
        if state[23] < -0.5:
            state[6] = 0.0
            state[10] = 0.0
            state[29] = 0.0
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

    # The tick estimate, hoisted so the placement, the ladder and the
    # staleness test all read the same number.
    step = state[25]

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

    # 20 bp of fast-vs-slow mid gap is a full unit of trend.  #16 showed
    # this term is worth ~0.002 of score either way; it is kept because
    # the base that carries it is the best mechanically clean record.
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

    # ---- THIS TRIAL'S CHANGE: the flow view expressed as SIZE -----------
    # sig in [-1, 1] tilts the two quotes about the 40-lot base:
    #   sig > 0 (buyers aggressive) -> large bid, small ask
    #   sig < 0 (sellers aggressive) -> small bid, large ask
    # Clamped to [8, 72] whole lots and rounded down to a whole lot
    # because O_SIZE is an integer field.  This costs no rows -- the size
    # is fixed at placement time -- and it never removes a side from the
    # book, which is the failure mode the record punishes hardest.
    bsz = 40.0 + 32.0 * sig
    if bsz > 72.0:
        bsz = 72.0
    if bsz < 8.0:
        bsz = 8.0
    bsz = float(int(bsz))
    asz = 40.0 - 32.0 * sig
    if asz > 72.0:
        asz = 72.0
    if asz < 8.0:
        asz = 8.0
    asz = float(int(asz))

    # ---- inventory lean --------------------------------------------------
    # The loose band of #4/#11, kept because #13's tighter one lost 0.037.
    # This, not the flow term, is what actually moves the ladder: at
    # |pos| = 150 lots it already outweighs a typical sig.
    inv = pos * 0.006
    if inv > 1.5:
        inv = 1.5
    if inv < -1.5:
        inv = -1.5
    desire = sig - 1.1 * inv

    # Volatility regime: >1 means the last 20 s moved more than the last
    # half hour did.  It stretches the ladder but never gates it (#8
    # tested gating and lost).
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
    # going the wrong way.
    pb = 0.0
    if desire < 0.0:
        pb = -desire
    pa = 0.0
    if desire > 0.0:
        pa = desire
    pb = pb + 0.45 * volx
    pa = pa + 0.45 * volx

    # Absolute backstop: past 200 lots the growing side is gone outright
    # and the reducing side sits at the touch, so an exit always exists.
    if pos > 200.0:
        pb = 2.0
        pa = 0.0
    if pos < -200.0:
        pa = 2.0
        pb = 0.0

    has_b = 0.0
    if state[5] > 0.0:
        has_b = 1.0
    has_a = 0.0
    if state[6] > 0.0:
        has_a = 1.0

    # ---- where each side wants to be ------------------------------------
    # touch -> one spread back -> absent.  The middle rung is kept: #14
    # deleted it and lost 0.037, so a shifted quote is worth more than no
    # quote.  Hysteresis: a side already stepped back only returns to the
    # touch once the pressure has genuinely fallen, and a side at the
    # touch only steps back once it has risen.
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

    # ---- QUEUE-PRIORITY PLACEMENT (#9's rule, unchanged) ----------------
    # A touch quote joins BEHIND the displayed size, so the only print
    # that ever fills it is the sweep that eats the level -- the worst
    # fill there is.  A price with nothing displayed has nothing ahead of
    # it, so the order fills on the FIRST print that reaches it.  At
    #     spread >= 3 ticks : bid at bid0 + step, ask at ask0 - step
    # both sides sit alone at the front and the round trip still captures
    # spread - 2*step >= step.  This is the only placement change that has
    # ever paid (+0.019), so it is the rule; nothing else fires.
    #
    # The threshold is 2.9 rather than 3.0 because prices are floats: a
    # true three-tick spread can compute a hair below 3.0 * step, and a
    # quote that silently stays at the touch is exactly the failure the
    # rule exists to remove.  'step' is the smallest spread ever seen,
    # hence a legal increment, so bid0 + step stays on the grid and no
    # row can be refused.
    #
    # Self-cross guard: an improved price never reaches the opposite
    # quote being placed in this same call, nor the opposite quote still
    # believed to be resting.  Both guards are inert at a three-tick
    # spread; they exist so a pathological book cannot emit a bad row.
    if step > 0.0:
        if spread >= 2.9 * step:
            if wb > 0.0:
                if wb >= bid0 - 0.000000001:
                    nwb = bid0 + step
                    okb = 1.0
                    if wa > 0.0:
                        if nwb > wa - 0.000000001:
                            okb = 0.0
                    else:
                        if state[6] > 0.0:
                            if nwb > state[6] - 0.000000001:
                                okb = 0.0
                    if okb > 0.5:
                        wb = nwb
            if wa > 0.0:
                if wa <= ask0 + 0.000000001:
                    nwa = ask0 - step
                    oka = 1.0
                    if wb > 0.0:
                        if nwa < wb + 0.000000001:
                            oka = 0.0
                    else:
                        if state[5] > 0.0:
                            if nwa < state[5] + 0.000000001:
                                oka = 0.0
                    if oka > 0.5:
                        wa = nwa

    # ---- pick-off protection --------------------------------------------
    # A quote the market has travelled THROUGH is stale and will be swept
    # at a price nobody else is on.  The tolerance differs by placement:
    # a touch quote is stale the moment the touch prints past it, while an
    # inside quote is improved by construction and only becomes stale when
    # the touch falls a further full tick away.
    sb = 0.0
    sa = 0.0
    if state[15] < 12.0:
        if ts - state[16] > 1000000000:
            if has_b > 0.5:
                tol = 0.000000001
                if state[28] > 0.5:
                    if step > 0.0:
                        tol = 1.5 * step
                if bid0 < state[5] - tol:
                    sb = 1.0
            if has_a > 0.5:
                tol = 0.000000001
                if state[29] > 0.5:
                    if step > 0.0:
                        tol = 1.5 * step
                if ask0 > state[6] + tol:
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
                    # after a pick-off, sit out a little: whatever swept
                    # us is often still working
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
        state[28] = 0.0
        if sb > 0.5:
            state[15] = state[15] + 1.0
            state[16] = float(ts)
    if ca > 0.5:
        state[6] = 0.0
        state[10] = 0.0
        state[29] = 0.0
        if sa > 0.5:
            state[15] = state[15] + 1.0
            state[16] = float(ts)

    pend = 0.0
    side1 = 0.0
    side2 = 0.0

    # The base lot size is 40 (0.004 BTC); here it is tilted by sig.
    # A resting order sits behind displayed size, so its fill probability
    # per lot does not depend on its own size while its P&L does -- which
    # is exactly what makes a conditional size the right place to put a
    # signal that cannot afford to spend rows on moving prices.
    if qb > 0.0:
        orders[count, O_OP] = OP_BUY
        orders[count, O_KIND] = KIND_LIMIT
        orders[count, O_TIF] = TIF_DAY
        orders[count, O_SIZE] = bsz
        orders[count, O_LIMIT] = qb
        orders[count, O_ORDER_ID] = 0.0
        count = count + 1
        state[5] = qb
        state[9] = 0.0
        # the flag is read off the price actually sent, so it cannot drift
        # from the order: above the touch by half a tick means inside.
        state[28] = 0.0
        if step > 0.0:
            if qb > bid0 + 0.5 * step:
                state[28] = 1.0
        pend = pend + 1.0
        side1 = 1.0

    if qa > 0.0:
        orders[count, O_OP] = OP_SELL
        orders[count, O_KIND] = KIND_LIMIT
        orders[count, O_TIF] = TIF_DAY
        orders[count, O_SIZE] = asz
        orders[count, O_LIMIT] = qa
        orders[count, O_ORDER_ID] = 0.0
        count = count + 1
        state[6] = qa
        state[10] = 0.0
        state[29] = 0.0
        if step > 0.0:
            if qa < ask0 - 0.5 * step:
                state[29] = 1.0
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
