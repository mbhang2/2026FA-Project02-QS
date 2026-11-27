def kernel(event, book, state, orders):
    # Market making on OKX BTC-USDT perp.
    #
    # The binding constraint is the 64 order-row action budget per session, so
    # this is a LOW-TOUCH maker: it posts a bid and an ask, leaves them resting
    # for a long time so they earn queue position and the spread, and only
    # replaces them when the flow signal or the price has really moved.
    #
    # Quotes are shifted by two things:
    #   * order flow  - a self-normalised, linearly decaying signed volume sum
    #                   kept in state (buy volume minus sell volume), mixed
    #                   with visible book imbalance.
    #   * inventory   - position leans both quotes away from the held side.
    # The only "aggressive" action is a small taker de-risk when inventory gets
    # close to the 1000-lot cap.
    #
    # state map (all floats):
    #  0 session id
    #  1 order rows already used this session
    #  2 wall time (s) of the last submission
    #  3 decayed signed trade volume (lots)
    #  4 wall time (s) of the last trade seen
    #  5 decayed absolute trade volume (lots)  -> normaliser for state[3]
    #  6 visible book imbalance in (-1, 1)
    #  7 mid at the time the resting quotes were anchored
    #  8..10 spare
    # 11 wall time (s) of the last inventory de-risk

    t = event[E_TS] * 1e-9
    sess = event[E_SESSION]
    pos = float(event[E_POSITION])
    nwork = int(event[E_N_WORKING])
    if nwork > 32:
        nwork = 32
    if nwork < 0:
        nwork = 0

    # ---- session roll: the action budget is per session ----
    if sess != state[0]:
        state[0] = float(sess)
        state[1] = 0.0
        state[2] = -1.0e9
        state[3] = 0.0
        state[5] = 0.0
        state[7] = 0.0

    # ---- order flow: linearly decaying signed / absolute volume ----
    # 900 s half-life-ish window: with requotes ~1 h apart we want a trend
    # read, not the last few seconds of noise.
    if event[E_KIND] == EVENT_TRADE:
        sz = float(event[E_SIZE]) * float(event[E_SIDE])
        dtt = t - state[4]
        if dtt < 0.0:
            dtt = 0.0
        if dtt > 900.0:
            state[3] = sz
            state[5] = abs(sz)
        else:
            f = 1.0 - dtt / 900.0
            state[3] = state[3] * f + sz
            state[5] = state[5] * f + abs(sz)
        state[4] = t

    # ---- need a two sided, fresh book to price anything ----
    if book[B_OK] < 0.5:
        return 0
    nb = int(book[B_N_BID])
    na = int(book[B_N_ASK])
    if nb < 1:
        return 0
    if na < 1:
        return 0
    bb = book[B_BID_PX]
    ba = book[B_ASK_PX]
    if bb <= 0.0:
        return 0
    if ba <= bb:
        return 0
    spread = ba - bb
    mid = 0.5 * (bb + ba)

    # ---- visible book imbalance across all filled levels ----
    sbid = 0.0
    sask = 0.0
    for i in range(5):
        if i < nb:
            sbid = sbid + book[B_BID_QTY + i]
        if i < na:
            sask = sask + book[B_ASK_QTY + i]
    if sbid + sask > 0.0:
        state[6] = (sbid - sask) / (sbid + sask)
    else:
        state[6] = 0.0

    acts = state[1]
    dt_last = t - state[2]
    moved = abs(mid - state[7])

    # ---- pacing: spread the remaining action budget over the rest of the day ----
    tod = t - 86400.0 * float(int(t / 86400.0))
    rem_t = 86400.0 - tod
    if rem_t < 60.0:
        rem_t = 60.0
    rem_a = 58.0 - acts
    if rem_a < 0.0:
        rem_a = 0.0
    n_re = rem_a / 4.0                      # cancels + replaces per requote
    if n_re < 1.0:
        n_re = 1.0
    gap = rem_t / n_re
    if gap < 30.0:
        gap = 30.0

    # ---- inventory de-risk with a small taker order near the cap ----
    if pos > 620.0 or pos < -620.0:
        if t - state[11] > 120.0 and acts + 1.0 <= 62.0:
            state[11] = t
            state[1] = acts + 1.0
            if pos > 0.0:
                orders[O_OP] = OP_SELL
            else:
                orders[O_OP] = OP_BUY
            orders[O_KIND] = KIND_MARKET
            orders[O_TIF] = TIF_DAY
            orders[O_SIZE] = 120.0
            orders[O_LIMIT] = 0.0
            return 1

    # ---- signals ----
    ofi_den = state[5] + 1.0
    sig_ofi = state[3] / ofi_den
    if sig_ofi > 1.0:
        sig_ofi = 1.0
    if sig_ofi < -1.0:
        sig_ofi = -1.0
    sig = 0.6 * sig_ofi + 0.4 * state[6]
    if sig > 1.0:
        sig = 1.0
    if sig < -1.0:
        sig = -1.0
    inv = pos / 1000.0

    # level offsets from the touch; 5 means "do not post this side"
    bid_off = 0
    ask_off = 0

    # aggressive buying -> pull the ask back (do not sell into a lift)
    if sig > 0.15:
        ask_off = 1
    if sig > 0.40:
        ask_off = 2
    if sig > 0.70:
        ask_off = 5
    # aggressive selling -> pull the bid back
    if sig < -0.15:
        bid_off = 1
    if sig < -0.40:
        bid_off = 2
    if sig < -0.70:
        bid_off = 5

    # inventory lean: long -> bid steps back (and eventually disappears),
    # short -> ask steps back.
    if inv > 0.15:
        bid_off = bid_off + 1
    if inv > 0.35:
        bid_off = bid_off + 1
    if pos > 350.0:
        bid_off = 5
    if inv < -0.15:
        ask_off = ask_off + 1
    if inv < -0.35:
        ask_off = ask_off + 1
    if pos < -350.0:
        ask_off = 5

    # ---- quote sizes, kept strictly inside the 1000 lot inventory cap ----
    sz_bid = 300.0
    room_b = 950.0 - pos
    if room_b < sz_bid:
        sz_bid = room_b
    if sz_bid < 1.0:
        bid_off = 5
    sz_ask = 300.0
    room_a = 950.0 + pos
    if room_a < sz_ask:
        sz_ask = room_a
    if sz_ask < 1.0:
        ask_off = 5

    if bid_off >= 5 and ask_off >= 5:
        return 0

    # ---- quote prices: sit on existing book levels where possible ----
    bid_px = 0.0
    if bid_off < 5:
        if bid_off < nb:
            bid_px = book[B_BID_PX + bid_off]
        else:
            bid_px = bb - (float(bid_off - nb) + 1.0) * spread
    ask_px = 0.0
    if ask_off < 5:
        if ask_off < na:
            ask_px = book[B_ASK_PX + ask_off]
        else:
            ask_px = ba + (float(ask_off - na) + 1.0) * spread

    # ---- when to requote ----
    want = 0
    cost = float(nwork) + 2.0
    if acts + cost <= 62.0:
        if nwork == 0:
            # nothing resting: get back in, but not too eagerly
            g2 = gap * 0.25
            if g2 < 30.0:
                g2 = 30.0
            if dt_last > g2:
                want = 1
        else:
            if dt_last > gap:
                want = 1
            elif dt_last > gap * 0.5 and moved > 3.0 * spread:
                want = 1

    if want == 0:
        return 0

    # ---- cancel everything we own, then post the two new quotes ----
    rows = 0
    for i in range(32):
        if i < nwork:
            orders[rows * 6 + O_OP] = OP_CANCEL
            orders[rows * 6 + O_ORDER_ID] = event[E_WORKING + i]
            rows = rows + 1

    if bid_off < 5:
        if bid_px > 0.0:
            orders[rows * 6 + O_OP] = OP_BUY
            orders[rows * 6 + O_KIND] = KIND_LIMIT
            orders[rows * 6 + O_TIF] = TIF_DAY
            orders[rows * 6 + O_SIZE] = float(int(sz_bid))
            orders[rows * 6 + O_LIMIT] = bid_px
            rows = rows + 1

    if ask_off < 5:
        if ask_px > 0.0:
            orders[rows * 6 + O_OP] = OP_SELL
            orders[rows * 6 + O_KIND] = KIND_LIMIT
            orders[rows * 6 + O_TIF] = TIF_DAY
            orders[rows * 6 + O_SIZE] = float(int(sz_ask))
            orders[rows * 6 + O_LIMIT] = ask_px
            rows = rows + 1

    state[1] = acts + float(rows)
    state[2] = t
    state[7] = mid
    return rows
