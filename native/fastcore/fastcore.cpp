// The native event core, one verified piece at a time.
//
// `fc_take_market_batch` is `BookQueueFill._take` for market orders under Amendments 20 and
// 23. The Python fill model remains the reference. This code is not used by a backtest until
// it has been shown to reproduce that model exactly.
//
// Plain C ABI: no Python headers are needed, and a Numba callback can use the same interface.

#include <cmath>
#include <cstdint>

namespace {

constexpr int kLevels = 4;  // levels 2 to 5; level 1 lives in the top-of-book columns
constexpr int8_t kWait = 0, kFilled = 1, kRejected = 2;

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

// False when a market order cannot fit in the five visible levels.
bool walk(const Book& b, int64_t row, bool buy, int64_t size, double* notional) {
    const double best = buy ? b.ask[row] : b.bid[row];
    const int64_t shown = buy ? b.ask_qty[row] : b.bid_qty[row];
    if (size <= shown) {
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
        const int64_t take = left < qty ? left : qty;
        total += static_cast<double>(take) * px;
        left -= take;
    }
    if (left > 0) return false;
    *notional = total;
    return true;
}

}  // namespace

extern "C" {

int32_t fc_abi_version() { return 1; }

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
    const Book b{ts, bid, ask, bid_qty, ask_qty, bid_px, bid_lots, ask_px, ask_lots, rows,
                 static_cast<double>(scale), max_age_ns};
    bool gaps = false;
    if (require_two_sided) {
        for (int64_t r = 0; r < rows && !gaps; ++r) gaps = one_sided(b, r);
    }
    int64_t filled = 0;
    for (int64_t i = 0; i < orders; ++i) {
        status[i] = kWait;
        fill_size[i] = 0;
        fill_price[i] = 0.0;
        fill_mid[i] = 0.0;
        fill_row[i] = -1;
        int64_t at = order_at[i] != 0 ? order_at[i] : tick_ts[i];
        int64_t row = fresh_row(b, at);
        if (row < 0 && at != tick_ts[i]) {
            at = tick_ts[i];
            row = fresh_row(b, at);
        }
        if (row < 0) continue;
        int64_t first, last;
        taker_rows(b, at, row, &first, &last);
        if (gaps && any_one_sided(b, first, last)) {
            if (at == tick_ts[i]) continue;
            at = tick_ts[i];
            row = fresh_row(b, at);
            if (row < 0) continue;
            taker_rows(b, at, row, &first, &last);
            if (any_one_sided(b, first, last)) continue;
        }
        const bool buy = side[i] > 0;
        bool have = false, refused = false;
        double worst = 0.0;
        int64_t worst_row = -1;
        for (int64_t r = first; r <= last; ++r) {
            double notional = 0.0;
            if (!walk(b, r, buy, size[i], &notional)) {
                refused = true;
                break;
            }
            const double price = notional / static_cast<double>(size[i]);
            if (!have || (buy && price > worst) || (!buy && price < worst)) {
                have = true;
                worst = price;
                worst_row = r;
            }
        }
        if (refused) {
            status[i] = kRejected;
            continue;
        }
        if (!have) continue;
        status[i] = kFilled;
        fill_size[i] = size[i];
        fill_price[i] = worst;
        fill_mid[i] = (b.bid[worst_row] + b.ask[worst_row]) / 2;
        fill_row[i] = worst_row;
        ++filled;
    }
    return filled;
}

}  // extern "C"
