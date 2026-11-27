def kernel(event, book, state, orders):
    # ==================================================================
    # BTC-USDT perpetual maker.  Twelfth proposal on the brief "shift the
    # quotes with order flow, so a trend does not run you over".
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
    #
    # THE ONE FACT THAT READS THE WHOLE TABLE.  Take the fill count
    # against the action cap.  The cap is 64 rows a session; the trials
    # fill 39-51 thousand times over 960 sessions, i.e. ~41 fills a day,
    # i.e. about 1.5 rows per fill.  Sixty-four rows a session is six
    # hundred and something rows across a day of 86,400 seconds: the
    # strategy re-prices roughly once every 90 minutes.  It is not that
    # the lean is mis-tuned; it is that THE LEAN IS NOT RUNNING.  A lean
    # only acts by cancelling a resting quote and replacing it, and the
    # budget is spent almost entirely on the replacements that follow
    # fills.  So every price-side variation -- #5, #6, #7, #8, #10 --
    # was a null result by construction: the mechanism they varied was
    # starved of the only resource that drives it.  That is exactly the
    # pattern the scores show: everything that changed WHEN or WHERE to
    # quote is noise around -0.05, and the only thing that ever moved was
    # #9's placement change, because placement costs no extra rows.
    #
    # COROLLARY.  Any further progress must come from a lever that costs
    # ZERO rows.  There are exactly two: the price a quote is sent to,
    # and the size it is sent with.  The price lever is #9's and is kept.
    # The SIZE lever has never been touched in eleven trials.  This trial
    # is the size lever, plus the one price change that is free once the
    # quote is already being sent.
    #
    # WHY SIZE IS THE RIGHT LEVER FOR "A TREND RUNS YOU OVER".  A trend
    # does not hurt a maker through the quotes it pulls; it hurts through
    # the inventory it accumulates while it is quoting.  Position is the
    # integral of (fill size) x (which side filled), over a day, and with
    # a flat 40/40 quote that integral is driven entirely by which side
    # the trend happened to hit.  Scaling the two sides by the position
    # makes the integral self-correcting on every single fill, at no row
    # cost, and it does so at the one moment the row budget is guaranteed
    # to be available -- the placement that follows a fill is going to
    # happen anyway.
    #
    # CHANGE 1: SIZE LEAN.  Base 40 lots.  The side that would GROW the
    # position shrinks and the side that would SHRINK it grows, linearly
    # in the position: 40 -/+ 0.10 * pos, clamped to [8, 64] lots, so a
    # 200-lot book runs 20 lots on the growing side against 60 on the
    # reducing side.  When flat it is the 40/40 quote of #4/#9, so this
    # change is silent in the state the earlier trials spent most of
    # their time in, and only bites exactly when the trend has started to
    # hurt.
    #
    # CHANGE 2: THE EXIT MAY CROSS HALF THE SPREAD.  #9 puts a quote one
    # tick inside the touch only when the spread is >= 3 ticks, because
    # that is the point at which a round trip still captures a whole
    # tick.  When inventory exists the objective is not capture, it is
    # getting out: the side that REDUCES the position is allowed to
    # improve by one tick at a two-tick spread, surrendering half of the
    # round trip in exchange for the front of the queue on the exit leg.
    # It is still a maker fill at 0 bp -- no market order appears in this
    # kernel under any condition -- and it is still alone at its price.
    # The growing side keeps #9's 3-tick rule.  With spread >= 3 ticks
    # both sides can improve and bid0 + step < ask0 - step still holds;
    # at exactly 2 ticks only the reducing side improves and it lands on
    # the mid, strictly between the two touches, so the two quotes can
    # never cross each other whatever the position sign.
    #
    # CHANGE 3 (mechanical, required by change 2): the pick-off test is
    # told which placement it is looking at.  #9 tested "has the best bid
    # printed below my bid", which is true by construction for a quote
    # that was deliberately placed one tick above the bid, so every
    # inside quote was killed within an event of arriving.  #11 showed
    # that correcting this is score-neutral on its own (-0.051 vs
    # -0.049, inside the noise), so it costs nothing and change 2 cannot
    # work without it.  state[28] / state[29] carry the flag, derived
    # from the price actually sent rather than from a branch, and are
    # cleared on every path that clears the quote belief.
    #
    # Nothing else moves.  The 30 s flow EWMA, the touch/depth imbalance,
    # the 2 min vs 30 min trend, the pressure combination, the
    # hysteresis, the inventory pull at 200 lots, the reactive pick-off
    # backstop, the in-flight guard, the 1 s evaluation throttle and the
    # day-paced row budget are #4/#9's, kept because they are
    # mechanically clean over 960 sessions: no refused rows, no invalid
    # cancels, no crossed quotes.
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
    #  25  step: smallest two-sided spread ever seen (a valid tick
    #      multiple); 0 until the first valid book arrives
    #  26  unused
    #  27  unused
    #  28  1 if the resting bid was placed INSIDE the touch, else 0
    #  29  1 if the resting ask was placed INSIDE the touch, else 0
    # ==================================================================
    ts = event[E_TS]
    sess = event[E_SESSION]

    # ---- session bookkeeping -------------------------------------------
    # TIF_DAY orders die with the old session, so quote beliefs, counters,
    # the inside/touch flags and the pick-off budget restart.  The
    # position, the decaying estimates and the step estimate carry across,
    # because the market never closed and the tick never changed.
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
    # All alphas are elapsed-time alphas, so a burst of book updates and a
    # sparse run integrate to the same estimate.  The volatility EWMAs
    # estimate E[|dmid|]; their ratio is ~1 when the tape is stationary.
    # The spread is tracked only as a running minimum: that is the tick
    # estimate both placement rules need, and every observed spread is an
    # integer multiple of it, so bid0 +/- step is always a legal price.
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

    # ---- aggressive order flow, kept entirely in our own memory ---------
    # Every trade event carries its aggressor and size, so there is no
    # unknown-side branch to write.  Signed and absolute volume decay with
    # the same elapsed-time (30 s) factor, so their ratio is a volume-
    # weighted average aggressor side in [-1, 1] over roughly the last
    # 30 s of tape.  This is #4's proven window; #7's faster one lost.
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
    # Fills are never reported; the only evidence is a working id we
    # believed in vanishing.  The belief -- price and inside/touch flag --
    # is dropped together, so the side becomes eligible to re-quote and
    # the next placement is sized against the new position.
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

    # The tick estimate, hoisted so the placement and the staleness test
    # both read the same number.
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
    # 200 so the position oscillates well inside the 1000-lot cap and no
    # order is ever silently refused.
    inv = pos * 0.006
    if inv > 1.5:
        inv = 1.5
    if inv < -1.5:
        inv = -1.5
    desire = sig - 1.1 * inv

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

    # Directional pressure on each side.  Positive = that side is in
    # trouble, i.e. a fill there is likely to be followed by the price
    # going the wrong way.  Both sides inherit the volatility term.
    pb = 0.0
    if desire < 0.0:
        pb = -desire
    pa = 0.0
    if desire > 0.0:
        pa = desire
    pb = pb + 0.45 * volx
    pa = pa + 0.45 * volx

    # Inventory override.  Past 200 lots the side that would GROW the
    # position is removed outright and the side that would SHRINK it is
    # pinned to the touch, so an exit always exists.
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
    # Base is the touch.  Hysteresis: a side already stepped back only
    # returns to the touch once the pressure has genuinely fallen, and a
    # side at the touch only steps back once it has risen.  Without the
    # gap the quote would flap and eat the whole row budget.
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

    # ---- QUEUE-PRIORITY PLACEMENT (kept from #9, extended) --------------
    # A touch quote joins BEHIND the whole displayed size, so the only
    # print that ever fills it is the sweep that eats the level.  At a
    # price with nothing displayed nothing is ahead of us, so the order
    # fills on the first trade that reaches it.  When the spread can pay
    # for it, step one valid tick inside the touch.
    #
    # The growing side needs a 3-tick spread, which leaves a round-trip
    # capture of spread - 2*step >= one step.  THE SIDE THAT REDUCES THE
    # POSITION IS ALLOWED TO DO IT AT 2 TICKS: it is exiting, not
    # earning, and half a spread paid for the front of the queue on the
    # exit leg is a trade worth making when a trend is what we are trying
    # to get away from.  At exactly 2 ticks only the reducing side
    # improves and it lands on the mid, strictly between the touches, so
    # the two quotes can never cross each other whatever the sign of the
    # position; at >= 3 ticks both may improve and bid0 + step <
    # ask0 - step still holds.
    need_b = 3.0 * step
    need_a = 3.0 * step
    if pos > 0.0:
        need_a = 2.0 * step
    if pos < 0.0:
        need_b = 2.0 * step
    if step > 0.0:
        if spread >= need_b:
            if wb > 0.0:
                if wb >= bid0 - 0.000000001:
                    wb = bid0 + step
        if spread >= need_a:
            if wa > 0.0:
                if wa <= ask0 + 0.000000001:
                    wa = ask0 - step

    # ---- SIZE LEAN (this trial's lever; costs no rows) ------------------
    # 40 lots base -- #2/#3 proved the base size is not a lever and the
    # whole eleven-trial record proves the base price is not either.
    # What has never been tried is asymmetry: the side that would GROW
    # the position shrinks, the side that would SHRINK it grows, linearly
    # in the position.  Flat, this is exactly the 40/40 quote of
    # #4/#9, so the change is silent in the state those trials lived in
    # and only bites once a trend has already started to build inventory.
    # It acts on every fill, at zero row cost, which is the only kind of
    # lever this run has left.
    lotsb = 40.0 - 0.10 * pos
    lotsa = 40.0 + 0.10 * pos
    if lotsb < 8.0:
        lotsb = 8.0
    if lotsb > 64.0:
        lotsb = 64.0
    if lotsa < 8.0:
        lotsa = 8.0
    if lotsa > 64.0:
        lotsa = 64.0
    lotsb = float(int(lotsb + 0.5))
    lotsa = float(int(lotsa + 0.5))
    if lotsb < 1.0:
        lotsb = 1.0
    if lotsa < 1.0:
        lotsa = 1.0

    # ---- pick-off protection --------------------------------------------
    # A quote the market has travelled THROUGH is stale and will be swept
    # at a price nobody else is on -- but "travelled through" means
    # different things for the two placements.
    #
    #   touch quote:   stale the moment the touch prints past it.
    #                  #4/#9's rule, exact.
    #   inside quote:  the touch printing past it is TRUE BY
    #                  CONSTRUCTION -- it is the price improvement we
    #                  deliberately bought.  Under #9's rule every inside
    #                  quote was flagged stale the instant its id appeared
    #                  and cancelled a millisecond later.  The real
    #                  staleness event is the touch falling a further full
    #                  tick away, i.e. past the old touch, which is what
    #                  the 1.5*step tolerance measures.
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

    # A resting order sits behind displayed size, so its fill probability
    # per lot does not depend on its own size while its P&L does.  That
    # is why the size lean is free: it re-weights fills that were going to
    # happen anyway, towards the side that flattens the book.
    if qb > 0.0:
        orders[count, O_OP] = OP_BUY
        orders[count, O_KIND] = KIND_LIMIT
        orders[count, O_TIF] = TIF_DAY
        orders[count, O_SIZE] = lotsb
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
        orders[count, O_SIZE] = lotsa
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
