// The native event core, one verified piece at a time.
//
// These functions implement `BookQueueFill` under Amendments 20 and 23. The Python fill model
// remains the reference. This code is not used by a backtest until it has been shown to
// reproduce that model exactly.
//
// Plain C ABI: no Python headers are needed, and a Numba callback can use the same interface.

#include <cmath>
#include <cstdint>
#include <limits>
#include <vector>

namespace {

constexpr int kLevels = 4;  // levels 2 to 5; level 1 lives in the top-of-book columns
constexpr int8_t kWait = 0, kFilled = 1, kRejected = 2;
constexpr double kPriceTol = 1e-9;

struct Book {
    const int64_t* ts;
    const double* bid;
    const double* ask;
    const int64_t* bid_qty;
    const int64_t* ask_qty;
    const int32_t* bid_px;
    const int32_t* bid_lots;
    const int32_t* ask_px;
    const int32_t* ask_lots;
    int64_t rows;
    double scale;
    int64_t max_age;
    bool require_two_sided;
    bool gaps;
};

struct Fill {
    int64_t size = 0;
    double price = 0.0;
    double mid = 0.0;
    int64_t row = -1;
    bool maker = false;
};

// Where a resting limit stands, as of `row`.
struct Resting {
    bool placed = false;
    int64_t session = 0;
    int64_t row = -1;
    bool has_queue = false;
    double queue = 0.0;
};

int64_t lower_bound(const int64_t* a, int64_t n, int64_t value) {
    int64_t lo = 0, hi = n;
    while (lo < hi) {
        const int64_t mid = lo + (hi - lo) / 2;
        if (a[mid] < value) lo = mid + 1; else hi = mid;
    }
    return lo;
}

int64_t upper_bound(const int64_t* a, int64_t n, int64_t value) {
    int64_t lo = 0, hi = n;
    while (lo < hi) {
        const int64_t mid = lo + (hi - lo) / 2;
        if (a[mid] <= value) lo = mid + 1; else hi = mid;
    }
    return lo;
}

// The last row at or before `at`, or -1 when none exists or it is too old.
int64_t fresh_row(const Book& b, int64_t at) {
    const int64_t row = upper_bound(b.ts, b.rows, at) - 1;
    if (row < 0 || at - b.ts[row] > b.max_age) return -1;
    return row;
}

bool one_sided(const Book& b, int64_t row) {
    return std::isnan(b.bid[row]) || std::isnan(b.ask[row]);
}

bool has_gaps(const Book& b) {
    if (!b.require_two_sided) return false;
    for (int64_t r = 0; r < b.rows; ++r) {
        if (one_sided(b, r)) return true;
    }
    return false;
}

// [first, last] are the rows sharing this millisecond and the state just before them.
void taker_rows(const Book& b, int64_t at, int64_t row, int64_t* first, int64_t* last) {
    const int64_t start = lower_bound(b.ts, b.rows, at);
    if (start <= row) {
        *first = start > 0 ? start - 1 : 0;
        *last = row;
    } else {
        *first = *last = row;
    }
}

bool any_one_sided(const Book& b, int64_t first, int64_t last) {
    for (int64_t r = first; r <= last; ++r) {
        if (one_sided(b, r)) return true;
    }
    return false;
}

// Lots and notional on the visible levels. False when a market order cannot fit.
bool walk(const Book& b, int64_t row, bool buy, int64_t size, bool has_limit, double limit,
          int64_t* got, double* notional) {
    const double best = buy ? b.ask[row] : b.bid[row];
    const int64_t shown = buy ? b.ask_qty[row] : b.bid_qty[row];
    if (has_limit && !(buy ? best <= limit + kPriceTol : best >= limit - kPriceTol)) {
        *got = 0;
        *notional = 0.0;
        return true;
    }
    if (size <= shown) {
        *got = size;
        *notional = best * static_cast<double>(size);
        return true;
    }
    int64_t left = size;
    double total = 0.0;
    if (shown > 0) {
        const int64_t take = left < shown ? left : shown;
        total += static_cast<double>(take) * best;
        left -= take;
    }
    const int32_t* prices = (buy ? b.ask_px : b.bid_px) + row * kLevels;
    const int32_t* lots = (buy ? b.ask_lots : b.bid_lots) + row * kLevels;
    for (int level = 0; left > 0 && level < kLevels; ++level) {
        const int64_t qty = lots[level];
        if (qty <= 0) continue;
        const double px = static_cast<double>(prices[level]) / b.scale;
        if (has_limit && (buy ? px > limit + kPriceTol : px < limit - kPriceTol)) break;
        const int64_t take = left < qty ? left : qty;
        total += static_cast<double>(take) * px;
        left -= take;
    }
    if (left > 0 && !has_limit) return false;
    *got = size - left;
    *notional = total;
    return true;
}

int8_t take(const Book& b, bool buy, int64_t remaining, int64_t at, int64_t tick_ts,
            bool has_limit, double limit, Fill* out) {
    int64_t row = fresh_row(b, at);
    if (row < 0 && at != tick_ts) {
        at = tick_ts;
        row = fresh_row(b, at);
    }
    if (row < 0) return kWait;
    int64_t first, last;
    taker_rows(b, at, row, &first, &last);
    if (b.gaps && any_one_sided(b, first, last)) {
        if (at == tick_ts) return kWait;
        at = tick_ts;
        row = fresh_row(b, at);
        if (row < 0) return kWait;
        taker_rows(b, at, row, &first, &last);
        if (any_one_sided(b, first, last)) return kWait;
    }
    bool have = false;
    int64_t worst_size = 0, worst_row = -1;
    double worst = 0.0;
    for (int64_t r = first; r <= last; ++r) {
        int64_t got = 0;
        double notional = 0.0;
        if (!walk(b, r, buy, remaining, has_limit, limit, &got, &notional)) return kRejected;
        if (got <= 0) continue;
        const double price = notional / static_cast<double>(got);
        if (!have || (buy && price > worst) || (!buy && price < worst)) {
            have = true;
            worst = price;
            worst_size = got;
            worst_row = r;
        }
    }
    if (!have) return kWait;
    out->size = worst_size;
    out->price = worst;
    out->mid = (b.bid[worst_row] + b.ask[worst_row]) / 2;
    out->row = worst_row;
    out->maker = false;
    return kFilled;
}

void place(const Book& b, bool buy, double limit, int64_t row, Resting* state) {
    const double best = buy ? b.bid[row] : b.ask[row];
    const int64_t qty = buy ? b.bid_qty[row] : b.ask_qty[row];
    if (std::fabs(best - limit) <= kPriceTol) {
        state->has_queue = true;
        state->queue = static_cast<double>(qty);
        return;
    }
    if (buy ? limit > best : limit < best) {
        state->has_queue = true;
        state->queue = 0.0;
        return;
    }
    state->has_queue = false;
    state->queue = 0.0;
}

// Bring the queue forward from `state->row` to `row`, from the book alone.
void advance(const Book& b, bool buy, double limit, int64_t row, Resting* state) {
    if (row <= state->row) return;
    const double* px = buy ? b.bid : b.ask;
    const int64_t* qty = buy ? b.bid_qty : b.ask_qty;
    const double sign = buy ? 1.0 : -1.0;
    const int64_t start = state->row + 1;
    int64_t last_worse = -1;
    for (int64_t r = row; r >= start; --r) {
        if (sign * (px[r] - limit) > kPriceTol) {
            last_worse = r;
            break;
        }
    }
    int64_t begin = start;
    if (last_worse >= 0) {
        state->has_queue = false;
        begin = last_worse + 1;
    }
    if (begin <= row) {
        bool better = false;
        for (int64_t r = begin; r <= row && !better; ++r) {
            better = sign * (limit - px[r]) > kPriceTol;
        }
        if (better) {
            state->has_queue = true;
            state->queue = 0.0;
        } else if (!state->has_queue) {
            state->has_queue = true;
            state->queue = static_cast<double>(qty[begin]);
        }
    }
    state->row = row;
}

int8_t limit_step(const Book& b, bool buy, double limit, int64_t remaining, int64_t eligible,
                  int64_t tick_ts, double price, int64_t size, int8_t tick_side, int64_t session,
                  Resting* state, Fill* out) {
    if (!state->placed) {
        int64_t at = eligible != 0 ? eligible : tick_ts;
        int64_t row = fresh_row(b, at);
        if (row < 0 && at != tick_ts) {
            at = tick_ts;
            row = fresh_row(b, at);
        }
        if (row < 0) return kWait;
        if (b.gaps && one_sided(b, row)) {
            if (at == tick_ts) return kWait;
            at = tick_ts;
            row = fresh_row(b, at);
            if (row < 0 || one_sided(b, row)) return kWait;
        }
        const bool crosses = buy ? limit >= b.ask[row] - kPriceTol : limit <= b.bid[row] + kPriceTol;
        state->placed = true;
        state->session = session;
        state->row = row;
        place(b, buy, limit, row, state);
        if (crosses) {
            const int8_t got = take(b, buy, remaining, at, tick_ts, true, limit, out);
            return got == kFilled ? kFilled : kWait;
        }
    }
    const int64_t row = upper_bound(b.ts, b.rows, tick_ts) - 1;
    if (state->session != session) {     // a GTC order meets a new session's book
        state->session = session;
        state->row = -1;
        state->has_queue = false;
        state->queue = 0.0;
    }
    advance(b, buy, limit, row, state);
    if (b.require_two_sided && (row < 0 || (b.gaps && one_sided(b, row)))) return kWait;
    const double mid = row < 0 ? std::numeric_limits<double>::quiet_NaN()
                               : (b.bid[row] + b.ask[row]) / 2;
    const bool through = buy ? price < limit - kPriceTol : price > limit + kPriceTol;
    if (through) {
        const int64_t got = remaining < size ? remaining : size;
        if (got <= 0) return kWait;
        out->size = got;
        out->price = limit;
        out->mid = mid;
        out->row = row;
        out->maker = true;
        return kFilled;
    }
    const bool hits = std::fabs(price - limit) <= kPriceTol && tick_side == (buy ? -1 : 1);
    if (!hits || !state->has_queue || fresh_row(b, tick_ts) < 0) return kWait;
    if (static_cast<double>(size) <= state->queue + kPriceTol) {
        state->queue -= static_cast<double>(size);
        return kWait;
    }
    const int64_t open = static_cast<int64_t>(
        std::floor(static_cast<double>(size) - state->queue + kPriceTol));
    const int64_t got = remaining < open ? remaining : open;
    state->queue = 0.0;
    if (got <= 0) return kWait;
    out->size = got;
    out->price = limit;
    out->mid = mid;
    out->row = row;
    out->maker = true;
    return kFilled;
}

Book make_book(const int64_t* ts, const double* bid, const double* ask, const int64_t* bid_qty,
               const int64_t* ask_qty, const int32_t* bid_px, const int32_t* bid_lots,
               const int32_t* ask_px, const int32_t* ask_lots, int64_t rows, int32_t scale,
               int32_t require_two_sided, int64_t max_age_ns) {
    Book b{ts, bid, ask, bid_qty, ask_qty, bid_px, bid_lots, ask_px, ask_lots, rows,
           static_cast<double>(scale), max_age_ns, require_two_sided != 0, false};
    b.gaps = has_gaps(b);
    return b;
}

struct Order {
    int64_t id;
    bool buy, market, gtc;
    int64_t size, filled;
    double limit;
    int64_t eligible_seq, eligible_ts;
    bool cancelling;
    int64_t cancel_at;
    Resting resting;
};

struct FillRow {
    int64_t seq, ts, session, order_id;
    int8_t side;
    int64_t size;
    double price;
    int8_t maker;
    double mid;
};

}  // namespace

extern "C" {

// What a strategy callback sees. It holds no array and no later event.
struct FcEvent {
    int64_t ts, seq, session;
    double price;
    int64_t size;
    int64_t position;
    int32_t side;
    int32_t actionable;
    int32_t n_working;
    int32_t book_ok;
    int64_t working[32];
    int32_t n_bid, n_ask;
    double bid_px[5], bid_qty[5], ask_px[5], ask_qty[5];
};

// op: 1 buy, 2 sell, 3 cancel. kind: 0 market, 1 limit. tif: 0 day, 1 good-till-cancelled.
struct FcAction {
    int32_t op, kind, tif, reserved;
    int64_t size;
    double limit;
    int64_t order_id;
};

typedef int32_t (*FcCallback)(const FcEvent*, FcAction*, int32_t, void*);

struct FcEngine {
    int64_t latency, max_age;
    int32_t scale, require_two_sided, action_cap, want_book;
    int64_t seq = -1, next_id = 1, position = 0, stepped = 0, callbacks = 0;
    std::vector<Order> working;
    std::vector<FillRow> fills;
    std::vector<FcAction> actions;
};

}  // extern "C"

namespace {

// Amendment 22: the latest row strictly before this millisecond, at most one second old.
void visible_book(const Book& b, int64_t now, FcEvent* event) {
    event->book_ok = 0;
    event->n_bid = event->n_ask = 0;
    const auto clear = [&]() {
        for (int i = 0; i < 5; ++i) {
            event->bid_px[i] = event->bid_qty[i] = event->ask_px[i] = event->ask_qty[i] = 0.0;
        }
    };
    clear();  // the event struct is reused, so no earlier book may survive in it
    const int64_t before = now - now % 1000000;
    const int64_t row = lower_bound(b.ts, b.rows, before) - 1;
    if (row < 0 || now - b.ts[row] > b.max_age) return;
    const auto copy = [&](bool bid, double* px, double* qty) {
        int count = 0;
        const int64_t top = bid ? b.bid_qty[row] : b.ask_qty[row];
        if (top > 0) {
            px[count] = bid ? b.bid[row] : b.ask[row];
            qty[count++] = static_cast<double>(top);
        }
        const int32_t* prices = (bid ? b.bid_px : b.ask_px) + row * kLevels;
        const int32_t* lots = (bid ? b.bid_lots : b.ask_lots) + row * kLevels;
        for (int level = 0; level < kLevels; ++level) {
            if (lots[level] <= 0) continue;
            px[count] = prices[level] < 0 ? std::numeric_limits<double>::quiet_NaN()
                                          : static_cast<double>(prices[level]) / b.scale;
            qty[count++] = static_cast<double>(lots[level]);
        }
        return count;
    };
    event->n_bid = copy(true, event->bid_px, event->bid_qty);
    event->n_ask = copy(false, event->ask_px, event->ask_qty);
    event->book_ok = event->n_bid > 0 && event->n_ask > 0;
    if (!event->book_ok) {     // ctx.book() is None here, so the kernel sees no levels
        event->n_bid = event->n_ask = 0;
        clear();
    }
}

void apply_actions(FcEngine* e, const FcAction* actions, int32_t count, int64_t ts, int64_t seq) {
    if (count > e->action_cap) count = e->action_cap;
    // Ids are given when the callback queues them, before any action is applied.
    int64_t next = e->next_id;
    for (int32_t i = 0; i < count; ++i) {
        const FcAction& a = actions[i];
        if (a.op == 1 || a.op == 2) {
            Order o{};
            o.id = next++;
            o.buy = a.op == 1;
            o.market = a.kind == 0;
            o.gtc = a.tif == 1;
            o.size = a.size;
            o.limit = a.limit;
            o.eligible_seq = seq + 1;
            o.eligible_ts = e->latency ? ts + e->latency : 0;
            e->working.push_back(o);
        } else if (a.op == 3) {
            for (size_t k = 0; k < e->working.size(); ++k) {
                Order& o = e->working[k];
                if (o.id != a.order_id) continue;
                if (e->latency) {
                    if (!o.cancelling) {
                        o.cancelling = true;
                        o.cancel_at = ts + e->latency;
                    }
                } else {
                    e->working.erase(e->working.begin() + static_cast<int64_t>(k));
                }
                break;
            }
        }
    }
    e->next_id = next;
}

}  // namespace

extern "C" {

int32_t fc_abi_version() { return 4; }

// status: 0 keeps waiting, 1 filled, 2 refused because it exceeds the visible levels.
int64_t fc_take_market_batch(const int64_t* ts, const double* bid, const double* ask,
                             const int64_t* bid_qty, const int64_t* ask_qty,
                             const int32_t* bid_px, const int32_t* bid_lots,
                             const int32_t* ask_px, const int32_t* ask_lots,
                             int64_t rows, int32_t scale, int32_t require_two_sided,
                             int64_t max_age_ns, const int64_t* order_at,
                             const int64_t* tick_ts, const int8_t* side, const int64_t* size,
                             int64_t orders, int8_t* status, int64_t* fill_size,
                             double* fill_price, double* fill_mid, int64_t* fill_row) {
    const Book b = make_book(ts, bid, ask, bid_qty, ask_qty, bid_px, bid_lots, ask_px, ask_lots,
                             rows, scale, require_two_sided, max_age_ns);
    int64_t filled = 0;
    for (int64_t i = 0; i < orders; ++i) {
        Fill fill;
        const int64_t at = order_at[i] != 0 ? order_at[i] : tick_ts[i];
        status[i] = take(b, side[i] > 0, size[i], at, tick_ts[i], false, 0.0, &fill);
        fill_size[i] = fill.size;
        fill_price[i] = fill.price;
        fill_mid[i] = fill.mid;
        fill_row[i] = fill.row;
        filled += status[i] == kFilled;
    }
    return filled;
}

FcEngine* fc_engine_new(int64_t latency_ns, int64_t max_age_ns, int32_t scale,
                        int32_t require_two_sided, int32_t action_cap, int32_t want_book) {
    FcEngine* e = new FcEngine();
    e->latency = latency_ns;
    e->max_age = max_age_ns;
    e->scale = scale;
    e->require_two_sided = require_two_sided;
    e->action_cap = action_cap;
    e->want_book = want_book;
    e->actions.resize(static_cast<size_t>(action_cap));
    return e;
}

void fc_engine_free(FcEngine* e) { delete e; }

int64_t fc_engine_run_session(FcEngine* e, const int64_t* ts, const double* bid, const double* ask,
                              const int64_t* bid_qty, const int64_t* ask_qty,
                              const int32_t* bid_px, const int32_t* bid_lots,
                              const int32_t* ask_px, const int32_t* ask_lots, int64_t rows,
                              const int64_t* trade_ts, const double* trade_price,
                              const int64_t* trade_size, const int8_t* trade_side,
                              int64_t trades, int64_t session, int64_t close_ts,
                              int32_t actionable, FcCallback callback, void* user) {
    const Book b = make_book(ts, bid, ask, bid_qty, ask_qty, bid_px, bid_lots, ask_px, ask_lots,
                             rows, e->scale, e->require_two_sided, e->max_age);
    FcEvent event{};
    for (int64_t t = 0; t < trades; ++t) {
        const int64_t seq = ++e->seq;
        const int64_t now = trade_ts[t];
        ++e->stepped;
        size_t keep = 0;
        for (size_t k = 0; k < e->working.size(); ++k) {
            Order& o = e->working[k];
            bool done = false;
            if (o.cancelling && now >= o.cancel_at) {
                done = true;
            } else if (seq >= o.eligible_seq && now >= o.eligible_ts) {
                Fill fill;
                const int64_t remaining = o.size - o.filled;
                int8_t status;
                if (o.market) {
                    const int64_t at = o.eligible_ts != 0 ? o.eligible_ts : now;
                    status = take(b, o.buy, remaining, at, now, false, 0.0, &fill);
                } else {
                    status = limit_step(b, o.buy, o.limit, remaining, o.eligible_ts, now,
                                        trade_price[t], trade_size[t], trade_side[t], session,
                                        &o.resting, &fill);
                }
                if (status == kRejected) {
                    done = true;
                } else if (status == kFilled) {
                    e->position += o.buy ? fill.size : -fill.size;
                    o.filled += fill.size;
                    e->fills.push_back(FillRow{seq, now, session, o.id,
                                               static_cast<int8_t>(o.buy ? 1 : -1), fill.size,
                                               fill.price, static_cast<int8_t>(fill.maker ? 1 : 0),
                                               fill.mid});
                    done = o.filled >= o.size;
                }
            }
            if (!done) {
                if (keep != k) e->working[keep] = o;
                ++keep;
            }
        }
        e->working.resize(keep);
        if (callback == nullptr) continue;
        event.ts = now;
        event.seq = seq;
        event.session = session;
        event.price = trade_price[t];
        event.size = trade_size[t];
        event.side = trade_side[t];
        event.position = e->position;
        event.actionable = actionable;
        event.n_working = 0;
        for (const Order& o : e->working) {
            if (event.n_working == 32) break;
            event.working[event.n_working++] = o.id;
        }
        if (e->want_book) visible_book(b, now, &event);
        ++e->callbacks;
        const int32_t count = callback(&event, e->actions.data(), e->action_cap, user);
        if (count < 0) return -1;
        if (actionable && count > 0) apply_actions(e, e->actions.data(), count, now, seq);
    }
    size_t keep = 0;
    for (size_t k = 0; k < e->working.size(); ++k) {
        Order& o = e->working[k];
        const bool cancelled = e->latency && o.cancelling && o.cancel_at <= close_ts;
        if (cancelled || !o.gtc) continue;
        if (keep != k) e->working[keep] = o;
        ++keep;
    }
    e->working.resize(keep);
    return e->stepped;
}

int64_t fc_engine_fill_count(const FcEngine* e) { return static_cast<int64_t>(e->fills.size()); }
int64_t fc_engine_position(const FcEngine* e) { return e->position; }
int64_t fc_engine_callbacks(const FcEngine* e) { return e->callbacks; }

void fc_engine_copy_fills(const FcEngine* e, int64_t* seq, int64_t* ts, int64_t* session,
                          int64_t* order_id, int8_t* side, int64_t* size, double* price,
                          int8_t* maker, double* mid) {
    for (size_t i = 0; i < e->fills.size(); ++i) {
        const FillRow& f = e->fills[i];
        seq[i] = f.seq;
        ts[i] = f.ts;
        session[i] = f.session;
        order_id[i] = f.order_id;
        side[i] = f.side;
        size[i] = f.size;
        price[i] = f.price;
        maker[i] = f.maker;
        mid[i] = f.mid;
    }
}

// A Numba kernel sees copies of one event and its visible book, plus its own state.
typedef int32_t (*FcKernel)(const int64_t*, const double*, double*, double*, int32_t, int32_t);

struct FcKernelUser {
    FcKernel kernel;
    double* state;
    int32_t state_len;
    int32_t error;
};

int32_t fc_kernel_callback(const FcEvent* event, FcAction* actions, int32_t cap, void* user) {
    FcKernelUser* k = static_cast<FcKernelUser*>(user);
    constexpr int kMaxOrders = 64;
    const int32_t room = cap < kMaxOrders ? cap : kMaxOrders;
    int64_t ints[40] = {event->ts, event->seq, event->session, event->size, event->position,
                        event->side, event->actionable, event->n_working};
    for (int i = 0; i < event->n_working; ++i) ints[8 + i] = event->working[i];
    double floats[24] = {event->price, static_cast<double>(event->book_ok),
                         static_cast<double>(event->n_bid), static_cast<double>(event->n_ask)};
    for (int i = 0; i < 5; ++i) {
        floats[4 + i] = event->bid_px[i];
        floats[9 + i] = event->bid_qty[i];
        floats[14 + i] = event->ask_px[i];
        floats[19 + i] = event->ask_qty[i];
    }
    double orders[kMaxOrders * 6] = {};
    const int32_t count = k->kernel(ints, floats, k->state, orders, k->state_len, room);
    if (count < 0) {
        k->error = 1;
        return -1;
    }
    if (count > room) {
        k->error = 2;
        return -1;
    }
    for (int32_t i = 0; i < count; ++i) {
        const double* row = orders + i * 6;
        const double op = row[0], kind = row[1], tif = row[2], size = row[3], limit = row[4], id = row[5];
        if (op == 1.0 || op == 2.0) {
            const bool valid = (kind == 0.0 || kind == 1.0) && (tif == 0.0 || tif == 1.0)
                && std::isfinite(size) && size >= 1.0 && size <= 9007199254740992.0
                && std::floor(size) == size
                && (kind == 0.0 || (std::isfinite(limit) && limit > 0.0));
            if (!valid) {
                k->error = 3;
                return -1;
            }
            actions[i] = FcAction{static_cast<int32_t>(op), static_cast<int32_t>(kind),
                                  static_cast<int32_t>(tif), 0, static_cast<int64_t>(size),
                                  kind == 1.0 ? limit : 0.0, 0};
        } else if (op == 3.0) {
            bool found = false;
            for (int w = 0; w < event->n_working && !found; ++w) {
                found = static_cast<double>(event->working[w]) == id;
            }
            if (!found) {
                k->error = 4;
                return -1;
            }
            actions[i] = FcAction{3, 0, 0, 0, 0, 0.0, static_cast<int64_t>(id)};
        } else {
            k->error = 3;
            return -1;
        }
    }
    return count;
}

// A fixed strategy for proving the event loop against the Python engine.
struct FcParityState {
    int64_t every, hold, offset, size, market_every, count;
};

int32_t fc_parity_strategy(const FcEvent* event, FcAction* actions, int32_t cap, void* user) {
    FcParityState* s = static_cast<FcParityState*>(user);
    const int64_t n = ++s->count;
    if (n % s->every != 0 || cap < 1) return 0;
    const int64_t step = n / s->every;
    if (event->n_working > 0 && step % s->hold == 0) {
        actions[0] = FcAction{3, 0, 0, 0, 0, 0.0, event->working[0]};
        return 1;
    }
    if (event->n_working >= 4) return 0;
    const bool buy = event->position < 0 || (event->position == 0 && step % 2 == 0);
    const bool market = step % s->market_every == 0;
    const double ticks = std::rint(event->price * 10.0);
    const double limit = market ? 0.0
        : (ticks + static_cast<double>(buy ? -s->offset : s->offset)) / 10.0;
    actions[0] = FcAction{buy ? 1 : 2, market ? 0 : 1, step % 3 == 0 ? 1 : 0, 0, s->size, limit, 0};
    return 1;
}

// Each limit order walks trades [start, end) until it is done. Returns the number of fills,
// or -1 if the output arrays are too small.
int64_t fc_run_limit_orders(const int64_t* ts, const double* bid, const double* ask,
                            const int64_t* bid_qty, const int64_t* ask_qty,
                            const int32_t* bid_px, const int32_t* bid_lots,
                            const int32_t* ask_px, const int32_t* ask_lots, int64_t rows,
                            int32_t scale, int32_t require_two_sided, int64_t max_age_ns,
                            const int64_t* trade_ts, const double* trade_price,
                            const int64_t* trade_size, const int8_t* trade_side,
                            const int8_t* side, const double* limit, const int64_t* size,
                            const int64_t* eligible, const int64_t* start, const int64_t* end,
                            int64_t orders, int64_t capacity, int64_t* out_order,
                            int64_t* out_trade, int64_t* out_size, double* out_price,
                            double* out_mid, int8_t* out_maker) {
    const Book b = make_book(ts, bid, ask, bid_qty, ask_qty, bid_px, bid_lots, ask_px, ask_lots,
                             rows, scale, require_two_sided, max_age_ns);
    int64_t count = 0;
    for (int64_t i = 0; i < orders; ++i) {
        Resting state;
        int64_t remaining = size[i];
        for (int64_t t = start[i]; t < end[i] && remaining > 0; ++t) {
            Fill fill;
            if (limit_step(b, side[i] > 0, limit[i], remaining, eligible[i], trade_ts[t],
                           trade_price[t], trade_size[t], trade_side[t], 0, &state,
                           &fill) != kFilled) {
                continue;
            }
            if (count >= capacity) return -1;
            out_order[count] = i;
            out_trade[count] = t;
            out_size[count] = fill.size;
            out_price[count] = fill.price;
            out_mid[count] = fill.mid;
            out_maker[count] = fill.maker ? 1 : 0;
            ++count;
            remaining -= fill.size;
        }
    }
    return count;
}

}  // extern "C"
