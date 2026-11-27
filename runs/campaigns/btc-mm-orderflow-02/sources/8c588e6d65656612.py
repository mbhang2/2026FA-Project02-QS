def kernel(event, book, state, orders):
    # ==================================================================
    # BTC-USDT perpetual market making, quoting one clip on each side.
    #
    # The claim being tested: a maker at the touch makes money after fees
    # once its quotes are shifted by order flow and by book imbalance, and
    # leaned away from whatever inventory it has picked up.
    #
    # Signals
    #   * aggressive order flow -- a running total of (buy lots - sell
    #     lots) over a ~2 s fade, normalised by a fast gross total and a
    #     much slower one so that (a) the number is dimensionless and
    #     (b) it bleeds back to zero during a lull instead of latching at
    #     its last burst value.
    #   * visible book imbalance across all five levels, weighted
    #     3/2/1/1/1 towards the touch.
    #   * the position itself, pushing both quotes away from the side we
    #     are already long/short.
    #
    # Quoting policy
    #   Each side is in one of three modes: OFF, ONE LEVEL BACK, AT THE
    #   TOUCH.  A resting quote is deliberately NOT chased when the touch
    #   moves -- the fill model makes us join behind the displayed size,
    #   so keeping our place in the queue is worth more than being one
    #   tick fresher.  A quote is replaced only when the signal changes
    #   its mode, or when the side has been filled and so no longer
    #   rests.  That keeps cancels rare and queue position intact.
    #
    # Memory map
    #   state[0]  fast signed flow (lots)          tau ~2 s
    #   state[1]  fast gross flow (lots)           tau ~2 s
    #   state[2]  timestamp of the previous call (ms)
    #   state[3]  session id
    #   state[4]  price our bid believes it rests at   (0 = nothing there)
    #   state[5]  price our ask believes it rests at   (0 = nothing there)
    #   state[6]  position at the previous call
    #   state[7]  time of the last order action (ms)
    #   state[10] slow gross flow (lots)           tau ~20 s
    #   state[16] bid mode in force (0 off / 1 back / 2 touch)
    #   state[17] ask mode in force (0 off / 1 back / 2 touch)
    #   state[20] lots bought so far (diagnostic)
    #   state[21] lots sold so far (diagnostic)
    # ==================================================================

    # ------------------------------------------------------------------
    # 1. clock, and the time based fade of the flow totals
    # ------------------------------------------------------------------
    now_ms = event[E_TS] * 0.000001
    last_ms = state[2]
    dt_ms = 0.0
    if last_ms > 0.0:
        dt_ms = now_ms - last_ms
    if dt_ms < 0.0:
        dt_ms = 0.0
    state[2] = now_ms

    if dt_ms > 0.0:
        kf = dt_ms / 2000.0
        if kf > 1.0:
            kf = 1.0
        kg = dt_ms / 20000.0
        if kg > 1.0:
            kg = 1.0
        state[0] = state[0] * (1.0 - kf)
        state[1] = state[1] * (1.0 - kf)
        state[10] = state[10] * (1.0 - kg)

    # ------------------------------------------------------------------
    # 2. every trade tells us its aggressor and its size
    # ------------------------------------------------------------------
    if event[E_KIND] == EVENT_TRADE:
        sz = event[E_SIZE] * 1.0
        sd = event[E_SIDE]
        if sd > 0:
            state[0] = state[0] + sz
        if sd < 0:
            state[0] = state[0] - sz
        state[1] = state[1] + sz
        state[10] = state[10] + sz

    # ------------------------------------------------------------------
    # 3. a new session wipes the venue's day orders, so forget them
    # ------------------------------------------------------------------
    sid = event[E_SESSION] * 1.0
    if sid != state[3]:
        state[3] = sid
        state[4] = 0.0
        state[5] = 0.0
        state[16] = 0.0
        state[17] = 0.0
        state[6] = event[E_POSITION] * 1.0
        state[7] = now_ms

    # ------------------------------------------------------------------
    # 4. fills are only ever visible through the position
    # ------------------------------------------------------------------
    pos = event[E_POSITION] * 1.0
    dp = pos - state[6]
    filled = 0.0
    if dp > 0.0:
        state[4] = 0.0          # our bid was taken, it no longer rests
        state[20] = state[20] + dp
        filled = 1.0
    if dp < 0.0:
        state[5] = 0.0          # our ask was taken
        state[21] = state[21] - dp
        filled = 1.0
    state[6] = pos

    # ------------------------------------------------------------------
    # 5. only act on a live two sided book, and only when orders count
    # ------------------------------------------------------------------
    if book[B_OK] < 0.5:
        return 0
    if event[E_ACTIONABLE] == 0:
        return 0

    b0 = book[B_BID_PX]
    a0 = book[B_ASK_PX]
    if b0 <= 0.0:
        return 0
    if a0 <= b0:
        return 0

    # ------------------------------------------------------------------
    # 6. the order flow signal, normalised so it is dimensionless
    # ------------------------------------------------------------------
    den = state[1] + 0.10 * state[10] + 100.0
    ofi = state[0] / den
    if ofi > 1.0:
        ofi = 1.0
    if ofi < -1.0:
        ofi = -1.0

    # ------------------------------------------------------------------
    # 7. the visible book imbalance, all five levels, touch weighted most
    # ------------------------------------------------------------------
    nb = int(book[B_N_BID])
    na = int(book[B_N_ASK])
    bq = 0.0
    aq = 0.0
    for i in range(5):
        w = 1.0
        if i == 0:
            w = 3.0
        if i == 1:
            w = 2.0
        if i < nb:
            bq = bq + book[B_BID_QTY + i] * w
        if i < na:
            aq = aq + book[B_ASK_QTY + i] * w
    bqi = 0.0
    if bq + aq > 0.0:
        bqi = (bq - aq) / (bq + aq)

    # ------------------------------------------------------------------
    # 8. combine, then lean away from the inventory we are carrying
    # ------------------------------------------------------------------
    skew = 0.55 * ofi + 0.45 * bqi
    inv = pos / 200.0
    if inv > 1.0:
        inv = 1.0
    if inv < -1.0:
        inv = -1.0
    skew = skew - 0.80 * inv
    if skew > 1.0:
        skew = 1.0
    if skew < -1.0:
        skew = -1.0

    # ------------------------------------------------------------------
    # 9. the prices a stepped back quote would sit at.  We use real book
    #    levels so the limit is always on the venue's own grid.
    # ------------------------------------------------------------------
    b1 = 0.0
    if nb >= 2:
        b1 = book[B_BID_PX + 1]
    if b1 <= 0.0:
        b1 = 0.0
    if b1 >= b0:
        b1 = 0.0
    a1 = 0.0
    if na >= 2:
        a1 = book[B_ASK_PX + 1]
    if a1 <= a0:
        a1 = 0.0

    # ------------------------------------------------------------------
    # 10. desired mode per side, with hysteresis so a signal parked on a
    #     threshold does not make us cancel and replace every millisecond
    # ------------------------------------------------------------------
    ub = 0.0 - skew          # how bearish it looks, read by the bid
    ua = skew                # how bullish it looks, read by the ask

    if state[16] == 2.0:
        db = 2
        if ub > 0.18:
            db = 1
        if ub > 0.50:
            db = 0
    elif state[16] == 1.0:
        db = 1
        if ub < 0.06:
            db = 2
        if ub > 0.50:
            db = 0
    else:
        db = 0
        if ub < 0.40:
            db = 1
        if ub < 0.06:
            db = 2

    if state[17] == 2.0:
        da = 2
        if ua > 0.18:
            da = 1
        if ua > 0.50:
            da = 0
    elif state[17] == 1.0:
        da = 1
        if ua < 0.06:
            da = 2
        if ua > 0.50:
            da = 0
    else:
        da = 0
        if ua < 0.40:
            da = 1
        if ua < 0.06:
            da = 2

    pb = 0.0
    if db == 2:
        pb = b0
    if db == 1:
        pb = b1
    if pb <= 0.0:
        db = 0

    pa = 0.0
    if da == 2:
        pa = a0
    if da == 1:
        pa = a1
    if pa <= 0.0:
        da = 0

    # hard stop well short of the 1000 lot inventory cap
    if pos > 900.0:
        pb = 0.0
        db = 0
    if pos < -900.0:
        pa = 0.0
        da = 0

    bp = state[4]
    ap = state[5]

    # ------------------------------------------------------------------
    # 11. a mode change means the old quote has to come off, and since we
    #     cannot tell which working id is which, that is a blanket cancel
    #     followed by a fresh pair at the levels the signal now wants.
    # ------------------------------------------------------------------
    cancel_all = 0.0
    if db != state[16]:
        cancel_all = 1.0
    if da != state[17]:
        cancel_all = 1.0
    force = 0.0
    if int(event[E_N_WORKING]) > 6:
        force = 1.0             # runaway guard: we only ever intend two
        cancel_all = 1.0

    if cancel_all > 0.5:
        if force < 0.5:
            if filled < 0.5:
                if now_ms - state[7] < 250.0:
                    return 0    # let the signal settle before churning again
        nw = int(event[E_N_WORKING])
        if nw > 32:
            nw = 32
        count = 0
        for i in range(32):
            if i < nw:
                orders[count, O_OP] = OP_CANCEL
                orders[count, O_ORDER_ID] = event[E_WORKING + i]
                count += 1
        if pb > 0.0:
            orders[count, O_OP] = OP_BUY
            orders[count, O_KIND] = KIND_LIMIT
            orders[count, O_TIF] = TIF_DAY
            orders[count, O_SIZE] = 10.0
            orders[count, O_LIMIT] = pb
            count += 1
        if pa > 0.0:
            orders[count, O_OP] = OP_SELL
            orders[count, O_KIND] = KIND_LIMIT
            orders[count, O_TIF] = TIF_DAY
            orders[count, O_SIZE] = 10.0
            orders[count, O_LIMIT] = pa
            count += 1
        state[4] = pb
        state[5] = pa
        state[16] = db * 1.0
        state[17] = da * 1.0
        state[7] = now_ms
        return count

    # ------------------------------------------------------------------
    # 12. no mode change: quietly re-arm any side that has been filled and
    #     is now dark, without disturbing the side still resting
    # ------------------------------------------------------------------
    count = 0
    if pb > 0.0:
        if bp <= 0.0:
            orders[count, O_OP] = OP_BUY
            orders[count, O_KIND] = KIND_LIMIT
            orders[count, O_TIF] = TIF_DAY
            orders[count, O_SIZE] = 10.0
            orders[count, O_LIMIT] = pb
            count += 1
            state[4] = pb
    if pa > 0.0:
        if ap <= 0.0:
            orders[count, O_OP] = OP_SELL
            orders[count, O_KIND] = KIND_LIMIT
            orders[count, O_TIF] = TIF_DAY
            orders[count, O_SIZE] = 10.0
            orders[count, O_LIMIT] = pa
            count += 1
            state[5] = pa
    if count > 0:
        state[7] = now_ms
    return count
