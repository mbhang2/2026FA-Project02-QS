def kernel(event, book, state, orders):
    # ==================================================================
    # Patient two-sided maker on OKX BTC-USDT perpetual, quoting away
    # from the touch.
    #
    # The question: does a resting quote placed several basis points
    # behind the touch get filled near the far end of a short excursion,
    # and does that excursion come back far enough to pay for the
    # adverse selection the fill implies?
    #
    # Score trail: #0 -0.1194, #1 -0.0358, #2 -0.0215 (all-maker, 21k
    # fills), #3 +1.0714 (73 fills).  The only positive trial is also
    # the one that almost never traded, so the honest reading of the
    # record is that the losing half of a maker book is the quote that
    # follows the touch down and gets filled in the middle of a move.
    # This trial keeps #3's skeleton -- every fill a maker fill,
    # volatility-scaled distance, inventory lean, rationed re-centring
    # -- and spends its changes on making the fills themselves rarer
    # and better selected:
    #
    #  * Wider floor.  10 bp (was 5 bp) behind the mid, or five seconds
    #    of typical movement, capped at 50 bp.  The claim under test is
    #    a quote that WAITS for a move; at 5 bp almost every fill was a
    #    queue fill in the middle of one rather than at its far end.
    #
    #  * Toxic flow widens the quote rather than merely shifting it.
    #    Distance is multiplied by (1 + 0.6*|signal|), the signal being
    #    five-level book imbalance blended with decaying aggressor-signed
    #    trade volume from E_SIDE/E_SIZE.  When one side is clearly in
    #    control the book is being repriced, not bounced, and the far
    #    quote should stand further back on BOTH sides.
    #
    #  * A post-fill cooldown on the ADDING side only.  After any fill,
    #    the side that would grow the position stays silent for two
    #    seconds; the side that would shrink it may post immediately.
    #    That is the cheapest cure for the classic maker death of
    #    reloading into the very move that just took the last quote,
    #    and it costs no action rows.
    #
    #  * Re-centring is cut to two per session.  A quote away from the
    #    touch does not need to follow the touch: moving it re-queues it
    #    behind everyone else at the new price and throws away the wait
    #    that the fill is supposed to be paid for.
    #
    #  * Inventory cap stays 600 lots (0.06 BTC) with 200-lot clips, so
    #    at most three adds.  Once a position exists the reducing quote
    #    is pinned just outside the spread, which is where an exit
    #    belongs, while the adding quote walks away from the market.
    #
    # All prices are checked against the self-imposed cap before they
    # are sent, so the venue never refuses an order and never burns an
    # id (and an action row) for nothing.
    #
    # state layout
    #   0 session id              1 action rows used this session
    #   2 live bid price          3 live ask price
    #   4 previous position       5 vol: ewma |1s mid change|, usd
    #   6 vol reference mid       7 vol reference ts
    #   9 last action ts         11 re-centres this session
    #  12 last fill ts           13 pending mask (1 bid, 2 ask, 3 both)
    #  18 flow numerator (decaying signed trade volume)
    #  19 flow denominator (decaying absolute trade volume)
    # ==================================================================
    count = 0

    sess = event[E_SESSION]
    if state[0] != sess:
        # the action budget and the re-centre ration are per session;
        # resting GTC quotes and the position survive the boundary
        state[0] = sess
        state[1] = 0.0
        state[11] = 0.0

    # ---- aggressor-signed trade flow, updated on trades only ---------
    # free of action cost, and the only sub-second information the trade
    # event carries: which side was paying up and how much
    if event[E_KIND] == EVENT_TRADE:
        sz = float(event[E_SIZE])
        if sz > 0.0:
            state[18] = 0.92 * state[18] + float(event[E_SIDE]) * sz
            state[19] = 0.92 * state[19] + sz

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

    # ---- recent movement: ewma of the one-second mid change ----------
    if state[7] == 0.0:
        state[7] = float(ts)
        state[6] = mid
        state[5] = 0.0003 * mid           # three bp per second to start
        state[4] = float(pos)

    if ts - int(state[7]) >= 1000000000:
        chg = mid - state[6]
        if chg < 0.0:
            chg = -chg
        state[5] = 0.85 * state[5] + 0.15 * chg
        state[6] = mid
        state[7] = float(ts)

    if event[E_ACTIONABLE] == 0:
        state[4] = float(pos)
        return 0

    # ---- infer fills from the position the venue shows ---------------
    prev = state[4]
    if pos > prev:
        if state[2] > 0.0:
            if float(pos - prev) >= 199.0:
                state[2] = 0.0            # that whole order is gone
        state[12] = float(ts)
    if pos < prev:
        if state[3] > 0.0:
            if float(prev - pos) >= 199.0:
                state[3] = 0.0
        state[12] = float(ts)
    state[4] = float(pos)

    # ---- a belief the venue contradicts must die ----------------------
    if state[13] == 0.0 and nw == 0 and ts - int(state[9]) > 100000000:
        state[2] = 0.0
        state[3] = 0.0

    # ---- how far behind the touch to rest -----------------------------
    d = 0.0010 * mid                      # 10 bp floor
    vd = 5.0 * state[5]                   # five seconds of typical move
    if vd > d:
        d = vd
    if d > 0.0050 * mid:
        d = 0.0050 * mid                  # 50 bp cap

    # ---- never post into a dislocated book ----------------------------
    if ask0 - bid0 > 0.0050 * mid:
        return 0

    # ---- visible book imbalance over all five levels ------------------
    bq = 0.0
    aq = 0.0
    nb = int(book[B_N_BID])
    na = int(book[B_N_ASK])
    for i in range(5):
        if i < nb:
            bq = bq + book[B_BID_QTY + i]
        if i < na:
            aq = aq + book[B_ASK_QTY + i]
    imbal = 0.0
    if bq + aq > 0.0:
        imbal = (bq - aq) / (bq + aq)

    flow = 0.0
    if state[19] > 0.5:
        flow = state[18] / state[19]

    sig = 0.5 * imbal + 0.5 * flow
    if sig > 1.0:
        sig = 1.0
    if sig < -1.0:
        sig = -1.0

    # one side in control means the book is repricing, not bouncing:
    # stand further back on both sides rather than pick a direction
    d = d * (1.0 + 0.6 * abs(sig))

    inv = float(pos) / 600.0
    if inv > 1.0:
        inv = 1.0
    if inv < -1.0:
        inv = -1.0

    # both quotes ride one reservation price: a bullish read lifts the
    # pair (so the bid is not left in front of a buyer cascade), and a
    # long position pulls the pair down, which drags the exit ask in
    # toward the spread and pushes the adding bid away
    shift = 0.40 * d * sig - 1.80 * d * inv

    want_bid = mid - d + shift
    want_ask = mid + d + shift
    if want_bid > mid - 0.30 * d:
        want_bid = mid - 0.30 * d
    if want_ask < mid + 0.30 * d:
        want_ask = mid + 0.30 * d
    # passivity: never improve the touch, never cross it
    if want_bid > bid0:
        want_bid = bid0
    if want_ask < ask0:
        want_ask = ask0
    # round outward onto the one-tick grid
    want_bid = int(want_bid * 10.0 + 0.0000001) / 10.0
    want_ask = int(want_ask * 10.0 + 0.9999999) / 10.0
    if want_bid <= 0.0 or want_ask <= want_bid:
        return 0

    # ---- act, strictly inside the 64-row session budget ---------------
    left = 64.0 - state[1]
    if left < 1.0:
        return 0
    size = 200.0

    place_bid = 0
    place_ask = 0
    do_cancel = 0

    if state[13] != 0.0:
        # a cancel is in flight; wait for it to land, then rebuild
        if ts - int(state[9]) < 1000000:
            return 0
        mask = state[13]
        state[13] = 0.0
        if mask == 1.0 or mask == 3.0:
            place_bid = 1
        if mask == 2.0 or mask == 3.0:
            place_ask = 1
        if left < 2.0:
            if place_bid == 1:
                place_ask = 0
            else:
                place_bid = 0
    else:
        lb = 0
        la = 0
        if state[2] > 0.0:
            lb = 1
        if state[3] > 0.0:
            la = 1
        if ts - int(state[9]) >= 1000000:
            if lb == 0 and la == 0:
                if nw == 0:
                    if left >= 2.0:
                        place_bid = 1
                        place_ask = 1
                else:
                    do_cancel = 1             # ids we do not account for
            elif lb == 0:
                if nw <= 1:
                    place_bid = 1
                else:
                    do_cancel = 1
            elif la == 0:
                if nw <= 1:
                    place_ask = 1
                else:
                    do_cancel = 1
            else:
                gb = want_bid - state[2]
                if gb < 0.0:
                    gb = -gb
                ga = want_ask - state[3]
                if ga < 0.0:
                    ga = -ga
                if nw > 2:
                    if state[11] < 2.0:
                        do_cancel = 1
                        state[11] = state[11] + 1.0
                elif (gb > 3.0 * d or ga > 3.0 * d) and state[11] < 2.0 and left >= 6.0:
                    # only a quote that has drifted a long way from where
                    # it should be is worth re-queueing; a stale quote
                    # away from the touch is a quote waiting for its move
                    do_cancel = 1
                    state[11] = state[11] + 1.0

    if do_cancel == 1:
        need = 4.0
        lim = 2
        if nw > 2:
            need = 9.0
            lim = 8
        if left < need:
            return 0
        for i in range(8):
            if i < nw:
                if i < lim:
                    orders[count, O_OP] = OP_CANCEL
                    orders[count, O_ORDER_ID] = event[E_WORKING + i]
                    count = count + 1
        state[2] = 0.0
        state[3] = 0.0
        state[13] = 3.0
        state[9] = float(ts)
        state[1] = state[1] + float(count)
        return count

    if place_bid == 1:
        ok = 1
        if float(count) >= left:
            ok = 0
        if float(pos) + size > 600.0:
            ok = 0                        # self-imposed cap, checked first
        if pos > 0 and ts - int(state[12]) < 2000000000:
            ok = 0                        # do not reload the adding side
        if ok == 1:
            orders[count, O_OP] = OP_BUY
            orders[count, O_KIND] = KIND_LIMIT
            orders[count, O_TIF] = TIF_GTC
            orders[count, O_SIZE] = size
            orders[count, O_LIMIT] = want_bid
            count = count + 1
            state[2] = want_bid
    if place_ask == 1:
        ok = 1
        if float(count) >= left:
            ok = 0
        if float(pos) - size < -600.0:
            ok = 0
        if pos < 0 and ts - int(state[12]) < 2000000000:
            ok = 0
        if ok == 1:
            orders[count, O_OP] = OP_SELL
            orders[count, O_KIND] = KIND_LIMIT
            orders[count, O_TIF] = TIF_GTC
            orders[count, O_SIZE] = size
            orders[count, O_LIMIT] = want_ask
            count = count + 1
            state[3] = want_ask

    if count > 0:
        state[9] = float(ts)
    state[1] = state[1] + float(count)
    return count
