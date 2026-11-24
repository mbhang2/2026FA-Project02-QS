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
                  int64_t tick_ts, double price, int64_t size, int8_t tick_side, Resting* state,
                  Fill* out) {
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
        state->row = row;
        place(b, buy, limit, row, state);
        if (crosses) {
            const int8_t got = take(b, buy, remaining, at, tick_ts, true, limit, out);
            return got == kFilled ? kFilled : kWait;
        }
    }
    const int64_t row = upper_bound(b.ts, b.rows, tick_ts) - 1;
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

}  // namespace

extern "C" {

int32_t fc_abi_version() { return 2; }

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
                           trade_price[t], trade_size[t], trade_side[t], &state,
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
