def kernel(event, book, state, orders):
    # ==================================================================
    # A patient two-sided maker on OKX BTC-USDT perpetual.
    #
    # Central claim under test: a resting quote placed a few basis points
    # behind the touch is filled near the far end of a short excursion,
    # and that excursion partly reverses, so the distance collected
    # exceeds the adverse selection that the fill implies.
    #
    # What changed from trial #1 (score -0.0358), and why:
    #
    #  * A crossing bug is removed.  In #1 the inventory skew could push
    #    the reducing quote through the mid (want_ask = mid - 0.5*d at
    #    full inventory).  A sell limit priced below the best bid is a
    #    TAKER, so #1 was quietly paying 2 bp exactly when it was most
    #    desperate to lighten.  Both quotes are now held at least 0.5*d
    #    on their own side of the mid and clamped behind the touch, so
    #    every fill is a maker fill at 0 bp.
    #
    #  * The taker tail-control market order is gone.  #0 showed that
    #    crossing out of a losing position is the most expensive thing
    #    this strategy can do.  The inventory cap bounds the exposure and
    #    a wider quote bounds the fill rate; a market order bounds
    #    neither, it only realises the worst print of the move.
    #
    #  * Distance is tighter: floor 4 bp (was 8 bp), volatility term 4x
    #    the one-second mean absolute mid move (was 6x), capped at 30 bp.
    #    At 8 bp a fill is mostly the middle of a move that keeps going;
    #    at 4-5 bp the short-horizon bid-ask bounce is a real part of the
    #    fill, which is the reversion the question asks about.
    #
    #  * Skew is stronger (2*d at the cap instead of 1.5*d) so a large
    #    inventory really pulls the reducing quote in and pushes the
    #    adding quote away.
    #
    #  * Re-centring goes through a pending slot: the cancel is sent now,
    #    the replacement one millisecond later, so the venue's inventory
    #    cap never sees the old and the new order simultaneously and
    #    silently refuses the new one.
    #
    #  * Budget discipline.  The run allows 64 order rows per session.
    #    Every fill costs one placement to restore that side.  Re-centres
    #    are rationed to four per session and fire only when a quote is
    #    1.5*d stale.  Stale quotes are otherwise left alone: a bid that
    #    waits for a dip is the whole idea.
    #
    # state layout
    #   0 session id            1 rows used this session
    #   2 live bid price        3 live ask price
    #   4 previous position     5 vol: mean |1 s mid change|
    #   6 vol reference mid     7 vol reference ts
    #   8 average entry price   9 last action ts
    #  10 (spare)              11 re-centres this session
    #  12 fills seen           13 pending mask (1 bid, 2 ask)
    # ==================================================================
    count = 0
    place_bid = 0
    place_ask = 0
    do_cancel = 0

    sess = event[E_SESSION]
    if state[0] != sess:
        # budget and counters are per session; resting GTC quotes and the
        # position survive, so state[2], state[3], state[8] are kept.
        state[0] = sess
        state[1] = 0.0
        state[11] = 0.0
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

    # ---- recent movement: ewma of the one-second mid change ----------
    if state[7] == 0.0:
        state[7] = float(ts)
        state[6] = mid
        state[5] = 0.00004 * mid          # four bp per second to start
        state[4] = float(pos)
        state[8] = 0.0
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

    # ---- infer fills from the position the venue shows ---------------
    prev = state[4]
    if pos != prev:
        if pos > prev:
            if state[2] != 0.0:
                if prev > 0.0 and pos > 0.0:
                    state[8] = (state[8] * prev + state[2] * (pos - prev)) / pos
                else:
                    state[8] = state[2]
                if (pos - prev) >= 199.0:
                    state[2] = 0.0     # that order is gone
        else:
            if state[3] != 0.0:
                if prev < 0.0 and pos < 0.0:
                    state[8] = (state[8] * prev + state[3] * (pos - prev)) / pos
                else:
                    state[8] = state[3]
                if (prev - pos) >= 199.0:
                    state[3] = 0.0
        state[12] = state[12] + 1.0
        state[4] = float(pos)
    if pos == 0:
        state[8] = 0.0

    # ---- a belief that the venue contradicts must die -----------------
    if state[13] == 0.0 and nw == 0 and ts - int(state[9]) > 50000000:
        state[2] = 0.0
        state[3] = 0.0

    # ---- how far behind the touch to rest ----------------------------
    d = 0.0004 * mid                       # 4 bp floor
    vd = 4.0 * state[5]                    # four seconds of typical move
    if vd > d:
        d = vd
    if d > 0.0030 * mid:
        d = 0.0030 * mid                   # 30 bp cap
    if d < 0.00015 * mid:
        d = 0.00015 * mid

    # ---- inventory skew: pull the reducing side in, push the adding
    #      side away, but never let either quote touch the mid --------
    skew = 2.0 * d * (float(pos) / 1000.0)
    if skew > 2.0 * d:
        skew = 2.0 * d
    if skew < -2.0 * d:
        skew = -2.0 * d

    want_bid = mid - d - skew
    want_ask = mid + d - skew
    if want_ask < mid + 0.5 * d:
        want_ask = mid + 0.5 * d
    if want_bid > mid - 0.5 * d:
        want_bid = mid - 0.5 * d
    # passivity: a limit that crosses is a taker and pays 2 bp
    if want_bid > bid0:
        want_bid = bid0
    if want_ask < ask0:
        want_ask = ask0
    # round outward onto the one-tick grid
    want_bid = int(want_bid * 10.0 + 0.0000001) / 10.0
    want_ask = int(want_ask * 10.0 + 0.9999999) / 10.0
    if want_bid <= 0.0 or want_ask <= want_bid:
        return 0

    # ---- act, strictly inside the session budget ---------------------
    left = 64.0 - state[1]
    if left < 1.0:
        return 0
    size = 200.0

    if state[13] != 0.0:
        # a cancel is in flight; wait for it to land, then rebuild
        if ts - int(state[9]) < 1000000:
            return 0
        if state[13] == 1.0 or state[13] == 3.0:
            if float(pos) + size <= 1000.0:
                place_bid = 1
        if state[13] == 2.0 or state[13] == 3.0:
            if float(pos) - size >= -1000.0:
                place_ask = 1
        state[13] = 0.0
    else:
        lb = state[2] > 0.0
        la = state[3] > 0.0
        if ts - int(state[9]) >= 1000000:
            if lb == 0 and la == 0:
                if nw == 0:
                    if left >= 2.0:
                        place_bid = 1
                        place_ask = 1
                else:
                    do_cancel = 1          # unknown ids at the venue
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
                    do_cancel = 1
                else:
                    if (gb > 1.5 * d or ga > 1.5 * d) and state[11] < 4.0 and left >= 6.0:
                        do_cancel = 1
                        state[11] = state[11] + 1.0
            if do_cancel == 1:
                if left < 6.0:
                    return 0           # cannot afford a clean rebuild
                c = 0
                for i in range(4):
                    if i < nw:
                        orders[count, O_OP] = OP_CANCEL
                        orders[count, O_ORDER_ID] = event[E_WORKING + i]
                        count = count + 1
                        c = c + 1
                state[2] = 0.0
                state[3] = 0.0
                state[13] = 3.0
                state[9] = float(ts)
                state[1] = state[1] + float(count)
                return count

    if place_bid == 1:
        orders[count, O_OP] = OP_BUY
        orders[count, O_KIND] = KIND_LIMIT
        orders[count, O_TIF] = TIF_GTC
        orders[count, O_SIZE] = size
        orders[count, O_LIMIT] = want_bid
        count = count + 1
        state[2] = want_bid
    if place_ask == 1:
        orders[count, O_OP] = OP_SELL
        orders[count, O_KIND] = KIND_LIMIT
        orders[count, O_TIF] = TIF_GTC
        orders[count, O_SIZE] = size
        orders[count, O_LIMIT] = want_ask
        count = count + 1
        state[3] = want_ask

    if count > 0:
        state[9] = float(ts)
    if count > 64:
        count = 64
    state[1] = state[1] + float(count)
    return count
