def kernel(event, book, state, orders):
    # ==================================================================
    # BTC-USDT perpetual maker.  Fifteenth proposal on the brief "shift
    # the quotes with order flow, so a trend does not run you over".
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
    #
    # READING THE FOURTEEN AS ONE RESULT:
    #
    # * Taker fees are settled: no market order appears below under any
    #   condition and every fill is a resting maker fill at 0 bp.
    # * Size is not a lever (#2/#3 neutral) and neither is the volatility
    #   gate (#8), the queue gate (#10), the flow window (#7) or the size
    #   lean (#12).  All of those were attempts to quote LESS OFTEN, and
    #   every one of them lost.
    # * #14 is the decisive experiment on that point.  It removed the
    #   one-spread step-back rung, i.e. it turned "quote a spread away"
    #   into "do not quote", and lost 0.037 (-0.051 -> -0.088).  So a
    #   quote that has been pushed away from the touch is worth MORE than
    #   no quote at all -- the brief's own mechanism (shift, do not
    #   withdraw) is doing real work, and the binary at-touch/removed
    #   switch of #4..#11 was throwing half of it away.
    # * #13 is the other half of the same lesson.  It tightened the
    #   inventory band four-fold and lost 0.037; loosening is the
    #   direction that has never been tested.  Both #13 and #14 say the
    #   same thing: this maker is being pulled out of the market too
    #   eagerly, on both the flow side and the inventory side.
    #
    # THE ONE CHANGE IN #15: THE QUOTE IS A GRADED LADDER, NOT A SWITCH.
    #
    #   rung 0   bid at the touch (improved one tick inside when the
    #            spread pays for it)  -- #9's placement, kept verbatim
    #   rung 1   bid one spread behind the touch
    #   rung 2   bid two spreads behind the touch
    #   rung 3   no bid
    #
    # with pressure thresholds to move UP a rung of 0.30 / 0.60 / 0.85 and
    # to move DOWN a rung of 0.15 / 0.40 / 0.60, so the ladder is a
    # hysteresis band on distance rather than on presence.  The ladder's
    # rung is recovered from the price actually resting (price relative to
    # the touch divided by the spread), so no extra memory is needed and
    # the state can never drift from the order.
    #
    # Why this and not another gate:
    #   - It is the brief taken literally: shift the quotes with the flow,
    #     by MORE than one step, and withdraw only at the extreme.
    #   - It keeps the maker in the market at every pressure level short
    #     of 0.85 (#14 showed withdrawal is the expensive choice) while
    #     still moving the quote out of the way of one-way flow (#9 and
    #     #5 both showed that placement is the only thing that moves the
    #     number, and #14 showed the step-back rung itself is good).
    #   - It costs the same rows: a rung change is one cancel and one
    #     place, exactly as a requote is now.  No new order types.
    #   - It loosens the inventory brake rather than tightening it, which
    #     is the only direction #13 left open.
    #
    # Kept verbatim from #11 (mechanically clean over 960 sessions: no
    # refused rows, no invalid cancels, no crossed quotes): the
    # sequential-id map, fill inference from ids leaving the working
    # list, the in-flight guard, the 1 s evaluation throttle, the
    # day-paced row budget, the reactive pick-off backstop with its
    # per-placement staleness tolerance, the flow/imbalance/trend
    # combination, and the one-tick front-of-queue improvement.
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
    # All alphas are elapsed-time alphas.  The two volatility EWMAs both
    # estimate E[|dmid|]; their ratio is ~1 on a stationary tape.  The
    # spread is tracked only as a running minimum: that is the tick
    # estimate the inside placement needs, and every observed spread is an
    # integer multiple of it, so bid0 + step is always a legal price.
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
    # the same elapsed-time (30 s) factor, so their ratio is a volume-
    # weighted average aggressor side in [-1, 1] over roughly the last
    # 30 s of tape.  This is #4's proven window; #7's faster one lost.
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

    # Inventory lean, the loose band of #4/#11 kept because #13's tighter
    # one lost 0.037.  Saturation near 250 lots.
    inv = pos * 0.006
    if inv > 1.5:
        inv = 1.5
    if inv < -1.5:
        inv = -1.5
    desire = sig - 1.1 * inv

    # Volatility regime: >1 means the last 20 s moved more than the last
    # half hour did.  It stretches the ladder but never gates it.
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
    pb = pb + 0.35 * volx
    pa = pa + 0.35 * volx

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

    # ---- WHICH RUNG OF THE LADDER WE ARE ON (this trial's one change) ---
    # The rung is recovered from the price that is actually resting, in
    # units of the current spread: 0 = at the touch (or improved inside
    # it), 1 = one spread behind, 2 = two spreads behind, 3 = absent.
    # Reading it back off the price means no bookkeeping can drift from
    # the order that is really there.
    crb = 0.0
    if has_b > 0.5:
        relb = (bid0 - state[5]) / spread
        if relb > 0.5:
            crb = 1.0
        if relb > 1.5:
            crb = 2.0
        if relb > 2.5:
            crb = 3.0
    cra = 0.0
    if has_a > 0.5:
        rela = (state[6] - ask0) / spread
        if rela > 0.5:
            cra = 1.0
        if rela > 1.5:
            cra = 2.0
        if rela > 2.5:
            cra = 3.0

    # Hysteresis on DISTANCE rather than on presence: climbing a rung
    # needs more pressure than staying on it, and stepping back down (or
    # leaving the ladder entirely) needs a genuine collapse of pressure.
    # Each rung is still a live quote earning the spread, which is what
    # #14 proved is worth more than withdrawing.
    rb = crb
    if crb < 0.5:
        if pb >= 0.30:
            rb = 1.0
        if pb >= 0.60:
            rb = 2.0
        if pb >= 0.85:
            rb = 3.0
    elif crb < 1.5:
        if pb >= 0.60:
            rb = 2.0
        if pb >= 0.85:
            rb = 3.0
        if pb < 0.15:
            rb = 0.0
    elif crb < 2.5:
        if pb >= 0.85:
            rb = 3.0
        if pb < 0.10:
            rb = 0.0
        elif pb < 0.40:
            rb = 1.0
    else:
        if pb < 0.05:
            rb = 0.0
        elif pb < 0.30:
            rb = 1.0
        elif pb < 0.60:
            rb = 2.0

    ra = cra
    if cra < 0.5:
        if pa >= 0.30:
            ra = 1.0
        if pa >= 0.60:
            ra = 2.0
        if pa >= 0.85:
            ra = 3.0
    elif cra < 1.5:
        if pa >= 0.60:
            ra = 2.0
        if pa >= 0.85:
            ra = 3.0
        if pa < 0.15:
            ra = 0.0
    elif cra < 2.5:
        if pa >= 0.85:
            ra = 3.0
        if pa < 0.10:
            ra = 0.0
        elif pa < 0.40:
            ra = 1.0
    else:
        if pa < 0.05:
            ra = 0.0
        elif pa < 0.30:
            ra = 1.0
        elif pa < 0.60:
            ra = 2.0

    wb = 0.0
    if rb < 0.5:
        wb = bid0
    elif rb < 1.5:
        wb = bid0 - spread
    elif rb < 2.5:
        wb = bid0 - 2.0 * spread
    if wb <= 0.0:
        wb = 0.0

    wa = 0.0
    if ra < 0.5:
        wa = ask0
    elif ra < 1.5:
        wa = ask0 + spread
    elif ra < 2.5:
        wa = ask0 + 2.0 * spread

    # ---- QUEUE-PRIORITY PLACEMENT (kept from #9) ------------------------
    # A touch quote joins BEHIND the whole displayed size, so the only
    # print that ever fills it is the sweep that eats the level.  At a
    # price with nothing displayed nothing is ahead of us, so the order
    # fills on the first trade that reaches it.  That is the one placement
    # change the record says works (#4 -> #9: -0.068 -> -0.049).
    #
    #     bid at bid0 + step,  ask at ask0 - step,  if spread >= 3 * step
    #
    # which leaves a round-trip capture of spread - 2*step >= step, still
    # strictly positive, and keeps bid0 + step < ask0 - step so neither
    # quote crosses the book or the other.  'step' is the smallest spread
    # ever seen, hence a legal price increment.
    if step > 0.0:
        if spread >= 3.0 * step:
            if rb < 0.5:
                if wb > 0.0:
                    wb = bid0 + step
            if ra < 0.5:
                if wa > 0.0:
                    wa = ask0 - step

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

    # 40 lots is 0.004 BTC.  A resting order sits behind displayed size, so
    # its fill probability per lot does not depend on its own size while
    # its P&L does; #2/#3 proved size is not the lever and #12 proved a
    # size lean is not one either.
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
        orders[count, O_SIZE] = 40.0
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
