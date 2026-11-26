def kernel(event, book, state, orders):
    # ==================================================================
    # Patient two-sided maker on OKX BTC-USDT perpetual, quoting away
    # from the touch.  The question: does a resting quote placed a few
    # basis points behind the touch get filled near the far end of a
    # short excursion, and does that excursion come back far enough to
    # pay for the adverse selection the fill implies?
    #
    # Score trail: #0 -0.1194, #1 -0.0358, #2 -0.0215.  The two big wins
    # were removing every crossing/market order (#2 is all-maker on both
    # sides) and tightening the distance.  What is left is a small
    # per-fill bleed, so this trial attacks the two things that decide
    # the sign of a maker book: WHICH fills you take, and HOW MUCH
    # inventory you let a trend load onto you.
    #
    # Changes from #2 (score -0.0215):
    #
    #  * Order flow now moves the reservation price.  Two signals, both
    #    free (no extra actions, no extra subscriptions):
    #       - visible book imbalance over all five levels,
    #       - a decaying aggressor-signed trade volume from E_SIDE/E_SIZE.
    #    Both are short-horizon momentum signals in this market.  The
    #    pair of quotes is shifted by up to 0.40*d in the direction of
    #    the signal, so a bid is not left sitting in front of a seller
    #    cascade and an ask is not left in front of a buyer cascade.
    #    That is the cheapest available cure for adverse selection: it
    #    costs zero order rows, which is the binding resource here
    #    (64 rows per session).
    #
    #  * Inventory is smaller and the leash is shorter.  Self-imposed
    #    cap 600 lots (0.06 BTC) instead of the venue's 1000.  #2 could
    #    be 0.10 BTC wrong on a trend day; that tail is what a maker
    #    dies of, and the question explicitly asks to survive trending
    #    sessions.  Size stays 200 lots, so three adds, not five.
    #
    #  * The inventory skew saturates the REDUCING quote near the mid
    #    after the first fill, which is intended: once long, the ask is
    #    the exit and it should be as close as passivity allows.  The
    #    adding quote keeps walking away: at the cap it is 3*d behind
    #    the mid.
    #
    #  * Distance widened a little: floor 5 bp, volatility term 5x the
    #    one-second mean absolute mid move, cap 40 bp.  #2 ran 4 bp / 4x
    #    / 30 bp; the question is about quoting "wide enough that price
    #    tends to come back", and at a 4 bp floor almost every fill was
    #    a queue fill in the middle of a move rather than the far end
    #    of one.
    #
    #  * A dislocation guard: if the visible spread is wider than 50 bp
    #    the book is not a book, and we stand aside instead of posting
    #    into a gap.
    #
    #  * Every placement is checked against the self-imposed cap before
    #    it is sent, so the venue never refuses one and never burns an
    #    id (and an action row) for nothing.
    #
    #  * Re-centring is rationed (3 per session) and always goes through
    #    the pending slot: cancel now, replace 1 ms later, so the venue
    #    never sees the old and the new order on the same side at once.
    #
    # state layout
    #   0 session id              1 order rows used this session
    #   2 live bid price          3 live ask price
    #   4 previous position       5 vol: ewma |1s mid change|, usd
    #   6 vol reference mid       7 vol reference ts
    #   9 last action ts         11 re-centres this session
    #  13 pending mask (1 bid, 2 ask, 3 both)
    #  18 flow numerator (decaying signed trade volume)
    #  19 flow denominator (decaying |trade volume|)
    # ==================================================================
    count = 0

    sess = event[E_SESSION]
    if state[0] != sess:
        # budget and rations are per session; resting GTC quotes and the
        # position survive a session boundary untouched
        state[0] = sess
        state[1] = 0.0
        state[11] = 0.0

    # ---- aggressor-signed trade flow, updated on trades only ---------
    if event[E_KIND] == EVENT_TRADE:
        sz = float(event[E_SIZE])
        if sz > 0.0:
            state[18] = 0.94 * state[18] + float(event[E_SIDE]) * sz
            state[19] = 0.94 * state[19] + sz

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
        state[5] = 0.0002 * mid           # two bp per second to start
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
    if pos < prev:
        if state[3] > 0.0:
            if float(prev - pos) >= 199.0:
                state[3] = 0.0
    state[4] = float(pos)

    # ---- a belief the venue contradicts must die ----------------------
    if state[13] == 0.0 and nw == 0 and ts - int(state[9]) > 100000000:
        state[2] = 0.0
        state[3] = 0.0

    # ---- how far behind the touch to rest -----------------------------
    d = 0.0005 * mid                      # 5 bp floor
    vd = 5.0 * state[5]                   # five seconds of typical move
    if vd > d:
        d = vd
    if d > 0.0040 * mid:
        d = 0.0040 * mid                  # 40 bp cap

    # ---- do not post into a dislocated book ---------------------------
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

    inv = float(pos) / 600.0
    if inv > 1.0:
        inv = 1.0
    if inv < -1.0:
        inv = -1.0

    # both quotes ride the same reservation price: a bullish read lifts
    # the bid toward the mid and pushes the ask away; a long position
    # does the mirror image
    shift = 0.40 * d * sig - 2.00 * d * inv

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
                    do_cancel = 1           # ids we do not account for
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
                    if state[11] < 4.0:
                        do_cancel = 1
                        state[11] = state[11] + 1.0
                elif (gb > 2.0 * d or ga > 2.0 * d) and state[11] < 3.0 and left >= 6.0:
                    # quotes far from where they should be: move them, but
                    # only a few times a session -- a stale quote away from
                    # the touch is a quote waiting for its move
                    do_cancel = 1
                    state[11] = state[11] + 1.0

    if do_cancel == 1:
        need = 4.0
        lim = 2
        if nw > 2:
            need = 10.0
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
        if float(pos) + size <= 600.0:
            orders[count, O_OP] = OP_BUY
            orders[count, O_KIND] = KIND_LIMIT
            orders[count, O_TIF] = TIF_GTC
            orders[count, O_SIZE] = size
            orders[count, O_LIMIT] = want_bid
            count = count + 1
            state[2] = want_bid
    if place_ask == 1:
        if float(pos) - size >= -600.0:
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
