def kernel(event, book, state, orders):
    # ==================================================================
    # A patient two-sided maker on OKX BTC-USDT perp.
    #
    # Central claim under test: a resting quote placed a few basis points
    # away from the touch is filled near the far end of a short excursion,
    # and BTC excursions of that size are partly reversed, so the distance
    # collected exceeds the adverse selection.
    #
    # Design decisions, and why they depart from the previous trial:
    #
    #  * No taker stop.  The previous kernel stopped out at 5*d, paying
    #    2 bp AND realising the worst print of the move.  The inventory
    #    cap already bounds the loss, so the stop was pure cost.  It is
    #    replaced by a very slow de-risking market order: only when the
    #    book is already at three quarters of the cap, the position is
    #    underwater, and it has been held 15 minutes.  That is a tail
    #    control, not a trading signal.
    #
    #  * Both sides are quoted all the time.  The previous kernel dropped
    #    the bid while long and the ask while short, which halves the fill
    #    rate for no reduction in trend risk.  Inventory is instead handled
    #    by skewing BOTH quotes: long pushes the pair down, so the ask
    #    walks toward the touch and the bid walks away.  At the cap the
    #    reducing side sits at the touch and the adding side is blocked.
    #
    #  * Never improve the visible touch.  bid <= bid0 and ask >= ask0, so
    #    every quote is strictly behind the best price.  That is the
    #    "away from the touch" premise and it also removes any chance of
    #    an accidental taker fill.
    #
    #  * Distance d is a floor of 8 bp raised by six times the mean
    #    absolute one-second mid change.  The floor is what the question
    #    calls "several ticks"; the volatility term backs the pair off in
    #    fast tape and pulls it in when the tape is quiet.
    #
    #  * Action budget.  The run allows 64 order rows per session, and the
    #    previous kernel burned them on stop-outs.  Here the only routine
    #    spend is (a) rebuilding a side that just filled and (b) a bounded
    #    number of re-centres.  Quotes that go stale are left alone: a
    #    stale bid is a bid that waits for a dip, which is the whole idea.
    #
    # state layout
    #   0  session id                 1  rows used this session
    #   2  price of the live bid      3  price of the live ask
    #   4  previous position          5  vol: mean |1 s mid change|, usd
    #   6  vol reference mid          7  vol reference timestamp
    #   8  position entry price       9  last placement timestamp
    #  10  anchor mid of the pair    11  (spare)
    #  12  fills seen                13  re-centres this session
    #  14  (spare)                   15  position opened timestamp
    # ==================================================================
    count = 0
    sess = event[E_SESSION]
    if state[0] != sess:
        # the budget is per session; positions and GTC quotes survive
        state[0] = sess
        state[1] = 0.0
        state[12] = 0.0
        state[13] = 0.0

    if book[B_OK] == 0.0:
        return 0
    bid0 = book[B_BID_PX]
    ask0 = book[B_ASK_PX]
    if bid0 <= 0.0 or ask0 <= 0.0 or ask0 <= bid0:
        return 0
    mid = 0.5 * (bid0 + ask0)
    ts = event[E_TS]
    pos = event[E_POSITION]
    nw = event[E_N_WORKING]

    # ---- realised movement: mean absolute 1-second mid change ----
    if state[7] == 0.0:
        state[7] = float(ts)
        state[6] = mid
        state[5] = 0.00003 * mid          # three bp per second to start
        state[10] = mid
        state[4] = float(pos)
        state[15] = float(ts)
    if ts - int(state[7]) >= 1000000000:
        chg = mid - state[6]
        if chg < 0.0:
            chg = -chg
        state[5] = 0.90 * state[5] + 0.10 * chg
        state[6] = mid
        state[7] = float(ts)

    if event[E_ACTIONABLE] == 0:
        state[4] = float(pos)
        return 0

    # ---- infer fills from the position the venue shows ----
    prev = state[4]
    if pos != prev:
        if pos > prev:
            if state[2] != 0.0:
                if prev > 0.0 and pos > 0.0:
                    state[8] = (state[8] * prev + state[2] * (pos - prev)) / pos
                else:
                    state[8] = state[2]
                if (pos - prev) >= 199.0:
                    state[2] = 0.0      # that order is gone
        else:
            if state[3] != 0.0:
                if prev < 0.0 and pos < 0.0:
                    state[8] = (state[8] * prev + state[3] * (pos - prev)) / pos
                else:
                    state[8] = state[3]
                if (prev - pos) >= 199.0:
                    state[3] = 0.0
        state[12] = state[12] + 1.0
        state[15] = float(ts)
    state[4] = float(pos)
    if pos == 0:
        state[8] = 0.0

    # ---- our belief must not outlive the venue's working list ----
    if nw == 0 and ts - int(state[9]) > 50000000:
        state[2] = 0.0
        state[3] = 0.0

    # ---- distance from the touch ----
    d = 0.0008 * mid                       # 8 bp floor
    vd = 6.0 * state[5]                    # six seconds of typical move
    if vd > d:
        d = vd
    if d > 0.0050 * mid:
        d = 0.0050 * mid                   # 50 bp cap
    if d < 0.0002 * mid:
        d = 0.0002 * mid

    # ---- slow tail control: only near the cap, only while losing ----
    if pos != 0 and ts - int(state[15]) > 900000000000:
        losing = 0
        if pos > 0:
            if mid < state[8] - 1.5 * d:
                losing = 1
        else:
            if mid > state[8] + 1.5 * d:
                losing = 1
        if losing == 1:
            if pos >= 600.0 or pos <= -600.0:
                if 62.0 - state[1] >= 1.0:
                    if pos > 0:
                        sz = 200.0
                        if float(pos) < 200.0:
                            sz = float(pos)
                        orders[count, O_OP] = OP_SELL
                        orders[count, O_KIND] = KIND_MARKET
                        orders[count, O_TIF] = TIF_DAY
                        orders[count, O_SIZE] = sz
                        count = count + 1
                    else:
                        sz = 200.0
                        if float(0 - pos) < 200.0:
                            sz = float(0 - pos)
                        orders[count, O_OP] = OP_BUY
                        orders[count, O_KIND] = KIND_MARKET
                        orders[count, O_TIF] = TIF_DAY
                        orders[count, O_SIZE] = sz
                        count = count + 1
                    state[15] = float(ts)
                    state[1] = state[1] + float(count)
                    return count

    # ---- where the pair wants to be ----
    skew = 1.5 * d * (float(pos) / 1000.0)
    if skew > 1.5 * d:
        skew = 1.5 * d
    if skew < -1.5 * d:
        skew = -1.5 * d

    want_bid = mid - d - skew
    want_ask = mid + d - skew
    if want_bid > bid0:                    # never improve the touch
        want_bid = bid0
    if want_ask < ask0:
        want_ask = ask0
    # round onto the venue's one-tick grid, outward on both sides
    want_bid = int(want_bid * 10.0 + 0.0000001) / 10.0
    want_ask = int(want_ask * 10.0 + 0.9999999) / 10.0
    if want_bid <= 0.0 or want_ask <= want_bid:
        return 0

    # ---- quoting decisions, strictly inside the action budget ----
    place_bid = 0
    place_ask = 0
    do_cancel = 0

    if state[1] < 52.0 and ts - int(state[9]) >= 1000000:
        if nw > 2 and ts - int(state[9]) > 50000000:
            do_cancel = 1              # stray orders: flatten the book of ids
        lb = state[2] != 0.0
        la = state[3] != 0.0
        if do_cancel == 1:
            state[2] = 0.0
            state[3] = 0.0
            place_bid = 1
            place_ask = 1
        else:
            if lb and la:
                gap = mid - state[10]
                if gap < 0.0:
                    gap = -gap
                # leave stale quotes alone unless the pair is far out of date
                if gap > 1.8 * d and state[13] < 8.0:
                    do_cancel = 1
                    state[13] = state[13] + 1.0
                    state[2] = 0.0
                    state[3] = 0.0
                    place_bid = 1
                    place_ask = 1
            else:
                if lb:
                    place_ask = 1
                else:
                    if la:
                        place_bid = 1
                    else:
                        place_bid = 1
                        place_ask = 1

    if do_cancel == 1:
        c = 0
        for i in range(32):
            if i < nw:
                if c < 4:
                    orders[count, O_OP] = OP_CANCEL
                    orders[count, O_ORDER_ID] = event[E_WORKING + i]
                    count = count + 1
                    c = c + 1

    if place_bid == 1:
        # position plus this lot must stay inside the 1000-lot cap
        if float(pos) + 200.0 <= 1000.0:
            orders[count, O_OP] = OP_BUY
            orders[count, O_KIND] = KIND_LIMIT
            orders[count, O_TIF] = TIF_GTC
            orders[count, O_SIZE] = 200.0
            orders[count, O_LIMIT] = want_bid
            count = count + 1
            state[2] = want_bid
    if place_ask == 1:
        if float(pos) - 200.0 >= -1000.0:
            orders[count, O_OP] = OP_SELL
            orders[count, O_KIND] = KIND_LIMIT
            orders[count, O_TIF] = TIF_GTC
            orders[count, O_SIZE] = 200.0
            orders[count, O_LIMIT] = want_ask
            count = count + 1
            state[3] = want_ask

    if place_bid == 1 and place_ask == 1:
        state[10] = mid                    # remember the anchor of this pair
    if count > 0:
        state[9] = float(ts)
    if count > 64:
        count = 64
    state[1] = state[1] + float(count)
    return count
