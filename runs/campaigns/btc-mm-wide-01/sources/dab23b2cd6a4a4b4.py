def kernel(event, book, state, orders):
    # ------------------------------------------------------------------
    # A two-sided maker that rests one quote pair far from the touch and
    # bets that the excursion which reaches the quote partly reverts.
    #
    #   * distance d is a basis-point floor (15 bp) raised by a 2-second
    #     realised-move estimate, so the pair backs off in fast markets;
    #   * only one unit of inventory is ever carried: while long we work
    #     only an exit ask (above the entry, never through the mid), and
    #     symmetrically while short, so a trend cannot average us in;
    #   * a hard stop at 5*d crosses the spread once, paying 2 bp, rather
    #     than carrying an unbounded mark-to-market loss;
    #   * the run facts give a 64-action session cap, so every returned
    #     row is metered in state[1] and the kernel simply stops acting
    #     when the budget is gone (8 rows are held back for the stop).
    #
    # state layout
    #   0  session id                1  rows used this session
    #   2  live bid price            3  live ask price
    #   4  previous position         5  vol ewma (usd per 2 s)
    #   6  vol reference mid         7  last vol timestamp
    #   8  entry price               9  last placement timestamp
    #  10  anchor mid at last pair   11  no-quote-until timestamp
    #  12  fill count                13  re-centres this session
    #  14  stop count
    # ------------------------------------------------------------------
    count = 0
    sess = event[E_SESSION]
    if state[0] != sess:
        # new session: the action budget is per session, the resting GTC
        # quotes and the position are not, so those are left alone.
        state[0] = sess
        state[1] = 0.0
        state[13] = 0.0

    if book[B_OK] == 0.0:
        return 0
    bid0 = book[B_BID_PX]
    ask0 = book[B_ASK_PX]
    if bid0 <= 0.0 or ask0 <= 0.0 or ask0 <= bid0:
        return 0
    mid = 0.5 * (bid0 + ask0)
    ts = event[E_TS]

    # --- short-horizon volatility: ewma of the 2-second mid move ---
    if state[7] == 0.0:
        state[7] = float(ts)
        state[6] = mid
        state[5] = 0.0001 * mid          # seed at one basis point
        state[10] = mid
        state[4] = float(event[E_POSITION])
    if ts - int(state[7]) >= 2000000000:
        chg = mid - state[6]
        if chg < 0.0:
            chg = -chg
        state[5] = 0.9 * state[5] + 0.1 * chg
        state[6] = mid
        state[7] = float(ts)

    if event[E_ACTIONABLE] == 0:
        state[4] = float(event[E_POSITION])
        return 0

    # --- infer fills from the position we are shown ---
    pos = event[E_POSITION]
    prev = state[4]
    if pos != prev:
        delta = pos - prev
        if delta > 0.0:
            if state[2] != 0.0:               # our bid was the live one
                if pos != 0 and prev > 0.0:
                    state[8] = (state[8] * prev + state[2] * delta) / pos
                else:
                    state[8] = state[2]
                state[2] = 0.0
        else:
            if state[3] != 0.0:               # our ask was the live one
                if pos != 0 and prev < 0.0:
                    state[8] = (state[8] * prev + state[3] * delta) / pos
                else:
                    state[8] = state[3]
                state[3] = 0.0
        state[4] = float(pos)
        state[12] = state[12] + 1.0
    if pos == 0:
        state[8] = 0.0
    if pos != 0 and state[8] <= 0.0:
        state[8] = mid                        # carry-over with no known entry

    # --- reconcile our belief about live orders with the venue ---
    nw = event[E_N_WORKING]
    if nw == 0 and ts - int(state[9]) > 10000000:
        if state[2] != 0.0 or state[3] != 0.0:
            state[2] = 0.0
            state[3] = 0.0

    # --- how far from the touch to rest ---
    d = 0.0015 * mid
    vd = 5.0 * state[5]
    if vd > d:
        d = vd
    if d > 0.03 * mid:
        d = 0.03 * mid
    if d < 0.0008 * mid:
        d = 0.0008 * mid

    # --- where the two quotes want to be ---
    want_bid = 0.0
    want_ask = 0.0
    if pos > 0:
        want_ask = state[8] + 0.5 * d         # exit above the entry
        if want_ask < mid + 0.3 * d:
            want_ask = mid + 0.3 * d          # never quote through the mid
    else:
        if pos < 0:
            want_bid = state[8] - 0.5 * d
            if want_bid > mid - 0.3 * d:
                want_bid = mid - 0.3 * d
        else:
            want_bid = mid - d
            want_ask = mid + d

    # --- hard stop: 5*d against the entry, cross out and cool off ---
    stop = 0
    if pos > 0 and mid < state[8] - 5.0 * d:
        stop = 1
    if pos < 0 and mid > state[8] + 5.0 * d:
        stop = -1
    if stop != 0:
        if 60.0 - state[1] < 3.0:
            return 0
        c = 0
        for i in range(32):
            if i < nw:
                if c < 2:
                    orders[count, O_OP] = OP_CANCEL
                    orders[count, O_ORDER_ID] = event[E_WORKING + i]
                    count += 1
                    c += 1
        if stop > 0:
            orders[count, O_OP] = OP_SELL
            orders[count, O_KIND] = KIND_MARKET
            orders[count, O_TIF] = TIF_DAY
            orders[count, O_SIZE] = float(pos)
        else:
            orders[count, O_OP] = OP_BUY
            orders[count, O_KIND] = KIND_MARKET
            orders[count, O_TIF] = TIF_DAY
            orders[count, O_SIZE] = float(0 - pos)
        count += 1
        state[2] = 0.0
        state[3] = 0.0
        state[8] = 0.0
        state[9] = float(ts)
        state[11] = float(ts + 5000000000)    # 5 s of quiet after a stop
        state[14] = state[14] + 1.0
        state[1] = state[1] + float(count)
        return count

    # --- quoting: at most one resting order per side ---
    if 60.0 - state[1] >= 8.0 and ts >= int(state[11]) and ts - int(state[9]) >= 2000000:
        nb = state[2] != 0.0
        na = state[3] != 0.0
        do_cancel = 0
        place_bid = 0
        place_ask = 0
        if pos > 0:
            if nb:                            # a stray bid while long
                do_cancel = 1
            if not na:
                place_ask = 1
        else:
            if pos < 0:
                if na:                        # a stray ask while short
                    do_cancel = 1
                if not nb:
                    place_bid = 1
            else:
                if nb and na:
                    gap = mid - state[10]
                    if gap < 0.0:
                        gap = -gap
                    # only re-centre when the pair is badly out of date
                    if gap > 4.0 * d and state[13] < 6.0:
                        do_cancel = 1
                        state[13] = state[13] + 1.0
                else:
                    if nb:
                        place_ask = 1
                    else:
                        if na:
                            place_bid = 1
                        else:
                            place_bid = 1
                            place_ask = 1
        if do_cancel == 1:
            # cancelling takes every id away, so rebuild what this
            # position actually wants to have resting
            state[2] = 0.0
            state[3] = 0.0
            if pos > 0:
                place_ask = 1
            else:
                if pos < 0:
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
                        count += 1
                        c += 1
        if place_bid == 1:
            px = int(want_bid * 10.0 + 0.5) / 10.0
            if px > 0.0:
                orders[count, O_OP] = OP_BUY
                orders[count, O_KIND] = KIND_LIMIT
                orders[count, O_TIF] = TIF_GTC
                orders[count, O_SIZE] = 200.0
                orders[count, O_LIMIT] = px
                count += 1
                state[2] = px
        if place_ask == 1:
            px2 = int(want_ask * 10.0 + 0.5) / 10.0
            if px2 > 0.0:
                orders[count, O_OP] = OP_SELL
                orders[count, O_KIND] = KIND_LIMIT
                orders[count, O_TIF] = TIF_GTC
                orders[count, O_SIZE] = 200.0
                orders[count, O_LIMIT] = px2
                count += 1
                state[3] = px2
        if place_bid == 1 and place_ask == 1:
            state[10] = mid
        if count > 0:
            state[9] = float(ts)

    if count > 60:
        count = 60
    state[1] = state[1] + float(count)
    return count
