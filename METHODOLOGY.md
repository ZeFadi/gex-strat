# GEX engine: methodology

Notation: spot $S$, strike $K$, time to settlement $T$ (years), implied vol $\sigma$, risk-free rate $r$, cost of carry $b$ of the expiry, forward $F = S e^{bT}$, discount factor $DF = e^{-rT}$, contract multiplier $m = 100$, open interest $OI$.

## 1. Clock

**Valuation time.** Every quantity is computed as of the chain's `asof`, never from the system clock. For CBOE, `asof` is the EARLIER of two New York timestamps in the file: the underlying's `last_trade_time` and the latest option `last_trade_time`. Both are 15 minutes behind in session and at the close after hours, where they agree to the second. If the index print ever ran ahead of the delayed option data, the earlier time keeps $T$ measured from when the option quotes were taken. Measuring from the later one would understate a 0DTE's $T$ and overstate its gamma (by about 13% with one hour left and a 15-minute error). Both timestamps are kept and shown in the data-quality table. Pre-open on 2026-10-13, the data is the 2026-10-12 close, so the 2026-10-13 SPXW options have $T$ = 23.75 h and are not 0DTE.

**Settlement time.**

| Root | Settles | Time value ends |
|---|---|---|
| SPXW, NDXP, RUTW, XSP, SPY, QQQ, IWM | close of the expiration date | 16:00 New York |
| SPX, NDX, RUT (AM monthlies) | special opening quotation (SOQ) of the expiration date | 09:30 New York |

Index options settle in cash; ETF options settle into shares. This only matters for scenarios that run past an expiry (section 8).

$T = (\text{settlement} - \text{asof}) / 365$ days (ACT/365, calendar time), computed in UTC (`surface.years_between`). Timestamps are converted with the tz database (`America/New_York`), so daylight saving is handled; the earlier fixed UTC-4 offset was wrong five months a year. Subtracting two New York datetimes directly would also be wrong: Python subtracts datetimes that share a tzinfo on the wall clock, one hour off for any expiry after the November clock change. Contracts with $T$ below one minute are settled and dropped. The share of open interest dropped this way is reported.

## 2. Forward and reference spot

**Forward per series** (root, expiry), from put-call parity on mid quotes:

$$F_i = K_i + \frac{C_{\text{mid}}(K_i) - P_{\text{mid}}(K_i)}{DF}, \qquad F = \operatorname{median}_{i \in \mathcal{K}} F_i$$

$\mathcal{K}$ is the set of the 3 strikes with the smallest $|C - P|$ among those quoted two-sided on both legs. The CBOE VIX rule uses one strike; using three keeps the estimate local but no longer sensitive to a single stale quote. A series with fewer than 3 such pairs takes $b$ interpolated in $T$ from the priced series (none on the fixture).

**Reference spot** $S^* = F_{\text{front}}\, e^{-r T_{\text{front}}}$. The vendor's `current_price` is not always the level the options were quoted against:

* SPY after the close carries the extended-hours print (777.735 vs a parity-implied 778.02);
* the SPX cash index stops at 16:00 while SPXW options trade until 16:15 (7,807.24 vs 7,803.05 on the fixture).

$S^*$ ignores the dividend yield over the front series' life, an error of $S\,q\,T_{\text{front}}$: 0.26 point on SPX for one day. The gap between $S^*$ and the vendor spot is reported in bp.

**Carry.** $b = \ln(F/S^*)/T$ per series. Dividends, repo, and the AM/PM settlement basis are all carried by $F$; none is assumed. On the fixture, $b$ is about 3.5% across tenors, consistent with rates minus the index yield.

## 3. Implied volatility

Black-76 on the forward:

$$V = DF\,[F N(d_1) - K N(d_2)] \;\text{(call)}, \quad d_{1,2} = \frac{\ln(F/K) \pm \tfrac12\sigma^2 T}{\sigma\sqrt T}$$

The inversion is a vectorized bisection on $[0.005, 5]$ with 60 halvings, to machine precision. Bisection always converges because price is monotone in $\sigma$; Newton fails in the wings, where vega vanishes.

**One vol per strike.** It is taken from the out-of-the-money mid (puts below $F$, calls at or above $F$) and applied to both the call and the put at that strike, since parity implies they share it. A price is inverted only if its time value exceeds both half the bid-ask spread and $0.01$. Below that threshold, the residual is rounding: a deep in-the-money 0DTE inverted to $\sigma = 0.95$ instead of 0.17 before this rule.

Fallbacks, in order: in-the-money mid (same threshold), vendor IV, then linear interpolation of $\sigma$ in $\ln(K/F)$ within the series (flat beyond the last quote). Each contract carries its `iv_source`. On the SPX fixture, open interest is priced as follows:

| Source | Share of OI |
|---|---|
| Mid | 90.4% |
| Vendor IV | 8.9% (mostly far wings and long-dated LEAPS with one-sided quotes) |
| Smile interpolation | 0.7% |
| Unpriced | 0% |

**Why not use the vendor's IV or Greeks directly.**

* CBOE rounds gamma to $10^{-4}$, about ±5% on an ATM SPX gamma.
* CBOE's IV is tied to its own clock and forward. On the AM-settled SPX of 2026-10-16, CBOE stops time at the last trading day's close and we run it to Friday's SOQ, which explains a 1.2 vol-point gap (ours lower) on that series only.
* Gamma at spot depends on total variance $\sigma^2 T$, which the price fixes. Implying our own $\sigma$ under our own $T$ therefore makes gamma insensitive to the time convention.

**Quote bounds.** The same out-of-the-money rule applied to the bid and to the ask gives $\sigma_{bid} \le \sigma \le \sigma_{ask}$: the vols the quotes cannot tell apart from the mid. Near the money on SPX, the median width is 0.09 vol point. Where the vol did not come from a quote, both bounds equal $\sigma$.

**ATM vol at a maturity** is interpolated linearly in total variance $\sigma_{ATM}^2 T$ across series, flat outside the listed maturities. The ATM vol of a series is the vol of the strike nearest its forward. The **one-day move** is $S^* \sigma_{ATM}(1/365) \sqrt{1/365}$: about ±30 points on SPX on the fixture.

## 4. Greeks

Generalized Black-Scholes, all Greeks with respect to spot for one option, with $N^\pm = N(d_1)$ for a call and $N(d_1) - 1$ for a put, and $d_1 = \frac{\ln(S/K) + (b + \sigma^2/2)T}{\sigma\sqrt T}$:

| Greek | Formula | Unit |
|---|---|---|
| Delta | $e^{(b-r)T} N^\pm$ | per unit |
| Gamma | $e^{(b-r)T}\, n(d_1) / (S\sigma\sqrt T)$ | per unit per point |
| Vanna $\partial\Delta/\partial\sigma$ | $-e^{(b-r)T}\, n(d_1)\, d_2/\sigma$ | per 1.00 of vol |
| Charm $\partial\Delta/\partial t$ | $-e^{(b-r)T}\left[(b-r)N^\pm + n(d_1)\left(\frac{b}{\sigma\sqrt T} - \frac{d_2}{2T}\right)\right]$ | per year of calendar time |

With $b = r - q$ these reduce to the Merton formulas used in the report and the first prototype; the prototype's charm was correct. `tests/test_pricing.py` checks each Greek against central finite differences of the price, over:

* moneyness from 80% to 120%;
* $T$ from 0.002 to 1 year;
* $\sigma$ of 12% and 45%;
* positive and negative carry.

## 5. Exposures

**Convention.** Dealers are long calls and short puts: $s_i = +1$ for a call, $-1$ for a put. This is an assumption about who holds the open interest, not an observation. Call and put legs are always shown separately.

| Measure | Per contract | Reads as |
|---|---|---|
| GEX | $s_i\, OI_i\, m\, \Gamma_i\, S^2/100$ | change in dealers' \$ delta for a +1% move in $S$ |
| VEX | $s_i\, OI_i\, m\, \text{vanna}_i\, S/100$ | change in dealers' \$ delta for +1 vol point |
| CHEX | $s_i\, OI_i\, m\, \text{charm}_i\, S/365$ | change in dealers' \$ delta over one calendar day |
| Gamma units | $s_i\, OI_i\, m\, \Gamma_i$ | units of underlying per 1-point move |

The hedge flow that neutralizes each is its negative: with GEX > 0, a rally leaves dealers longer, and they sell.

Every function also accepts an explicit dealer position per contract in place of $s_i\,OI_i$. That is how flow-signed GEX (section 9) reuses the same code path. Under the convention, magnitudes are an UPPER bound: the convention gives dealers all of the open interest, including customer-to-customer positions and positions that are hedged elsewhere.

The prototype's `share_gex` $= \Gamma\, OI\, m\, S$ was labeled "shares per \$1" but was dollars per point. The quantity in units is $\Gamma\, OI\, m$.

## 6. Levels

* **GEX profile** $G(S) = \sum_i s_i\, OI_i\, m\, \Gamma_i(S)\, S^2/100$ on a 241-point grid over $S^* (1 \pm 6\%)$.
  * Sticky strike: each contract keeps its $\sigma$ and $b$ while $S$ moves, and time is frozen.
  * The same function produces the net GEX, the strike bars and the curve, so $G(S^*)$ equals the reported net GEX to machine precision. This is tested.
  * In the prototype, the curve at spot was 97.4bn against 86.8bn reported, because the two paths used different vol fallbacks.
* **Zero gamma.** Every sign change of $G$ on the grid is solved with Brent's method on the exact function ($x_{tol} = 10^{-6}$ point). All roots are reported; the headline is the one nearest spot. With no sign change within ±6%, the page says so rather than extrapolating. The tests check a closed form: one call at $K_1$ and one put at $K_2$ with equal OI, flat vol and zero carry have $S^\star = \sqrt{K_1 K_2}\, e^{-\sigma^2 T/2}$.
* **Walls.** The call wall is the strike with the largest call GEX at or above spot; the put wall is the strike with the most negative put GEX at or below spot. The definition is symmetric. Unrestricted, the put extreme on the fixture is the in-the-money 8,000 strike, which cannot act as support. The unrestricted extreme is shown as "max |γ|".
* **Max pain** $= \arg\min_{X \in \text{strikes}} \sum_i OI_i\,(\text{intrinsic}_i \text{ at } X)$, for one expiry date only. It is undefined, and not shown, across expiries. On a third-Friday date view, AM and PM roots settle on two different prices and max pain assumes one.
* **Distances in one-day sigmas.** Every level is also given as $(L - S^*) / \text{one-day move}$. A 0.5% distance means little at 25% vol and a lot at 8%; in sigmas it is comparable across regimes.
* **Trapdoor.** Flagged when spot is below zero gamma AND the put wall is within one one-day sigma below spot. In that configuration, dealers short puts sell into the decline and the support turns into an accelerator. The threshold is `config.TRAPDOOR_SIGMAS`.
* **Zero-gamma band.** The book (OI) is exact; what is uncertain is how vols move with spot, how precisely the quotes fix them, and which expiries are meant. One deterministic variant answers each question; a bootstrap would need an invented noise model.

  | Variant | Question |
  |---|---|
  | Base | sticky strike, mid vols |
  | Sticky moneyness | the smile moves with the forward: ATM vol stays put, a fixed strike's vol falls as spot falls on an equity skew (the opposite of sticky strike, where ATM vol rises) |
  | Vols at bid / at ask | quote precision |
  | Without expiries > 1 year | long-dated OI with stale or one-sided quotes |
  | Without the front expiry | how much one expiry (often 0DTE) drives the level |

  The band is the range of the variants' zero gammas. The regime is **indeterminate** when the band contains spot, or when the variants disagree on the sign of GEX at spot. On the SPX fixture the band is 7,733 to 7,754, with spot at 7,801: the front expiry moves the level by 19 points, and quote precision by less than 1.

## 7. Front expiry (0DTE)

The front is the first live expiry date. It is labeled 0DTE only if it settles on the as-of date.

The former Pin Score combined hand-picked weights (50/30/20, ×0.4 in negative gamma) into a 0-100 figure that read like a probability and was not one. It is replaced by its components:

* hours to settlement;
* ATM implied vol;
* the 1σ move to settlement, $S^*\sigma_{ATM}\sqrt T$;
* the strike with the largest |net GEX| within 2σ, with its distance in points and in σ, and its share of the front's |GEX|;
* the front's share of the book's |GEX| and of session volume.

**Projection to the close.** The front expiry's GEX by strike is re-evaluated at 12:00, 14:00, 15:00, 15:30 and 15:50 New York on its expiry day, with spot and vols held and $T$ shrinking. As $T \to 0$, gamma concentrates on the strikes nearest spot (a Dirac at settlement). This shows where the end-of-day hedging will sit, which a single snapshot does not. On the fixture, front net GEX goes from 7.8bn now to 27bn at 15:50.

## 8. Scenarios

Each scenario re-prices the delta of every contract in the view (no Taylor expansion), under sticky strike with carry held:

$$\Delta D = \sum_i pos_i\, m\,[\Delta_i(S', \sigma_i + \delta\sigma, T_i - \delta t) - \Delta_i(S, \sigma_i, T_i)], \qquad \text{hedge flow} = -\Delta D$$

| Shock | Values |
|---|---|
| Implied vol, parallel | −5, −2, +2, +5 vol points |
| Spot, sticky strike | −2%, −1%, +1%, +2% |
| Time | to the next 16:00 New York close, +1 calendar day, +3 calendar days |

A contract that settles inside the time step:
* if cash-settled (index), leaves the book: its delta becomes 0 and the hedge is unwound;
* if physically settled (ETF), becomes shares: its delta becomes ±1 if in the money, 0 otherwise.

Flows are given in \$ and in hedge units:
* indices: futures contracts, $\text{units} / (\text{multiplier} \times \text{ratio})$, with ES at \$50 (ratio 10 for XSP), NQ at \$20 and RTY at \$50, futures taken at spot;
* ETFs: shares.

The tests check that tiny shocks reproduce GEX and VEX (to $10^{-4}$), and the two settlement rules.

## 9. Flow-signed GEX

Dealer positions come from today's signed trades instead of the OI convention, starting from zero at 09:30:

$$pos_c = -\sum_{\text{trades of } c} \text{sign} \times \text{size}$$

| Rule | Sign of a trade |
|---|---|
| Lee-Ready (1991) | above the prevailing mid: buy; below: sell; at the mid: tick test (zero ticks take the last non-zero tick) |
| EMO (Ellis, Michaely, O'Hara 2000) | at the ask: buy; at the bid: sell; elsewhere: tick test |
| BVC (Easley, López de Prado, O'Hara 2012) | per time bar, buy share of volume $= \Phi(\Delta P / \sigma_{\Delta P})$, with $\sigma_{\Delta P}$ the contract's own bar-to-bar std |

**Outputs.**
* Structural GEX (yesterday's OI, by convention), flow GEX (today's inventory) and their sum, by strike.
* **Wall pressure**: flow GEX of the strikes within one one-day sigma of each wall, divided by the absolute structural GEX there. Below −1, the session's flow has more than offset yesterday's dealer gamma around the wall, so the wall no longer acts as the level the structural view shows. This is the report's "sweeps into a call wall" divergence.

**Input.** A trade file with `contract, time, price, size, bid, ask`, the quote being the one in force at the trade. Any OPRA source can produce it. The CBOE delayed chain cannot, and is never used for this.

## 10. History and study

**Archive** (`python cli.py archive`, scheduled by `python cli.py install`: a strict evening run on weekdays at 16:30 New York, where a symbol not archived is an error in the log, and a lenient catch-up run at 01:30 New York, both converted to the machine's local time; network errors are retried 3 times). It stores:
* the raw CBOE payload per symbol and day (`data/raw/{SYMBOL}/{date}.json.gz`, about 1-2 MB);
* one summary row per symbol and day in `data/history.csv`: levels, band, front metrics, the day's OHLC and price change, the vendor's iv30, and the generation time of the CBOE file it was computed from (`file_generated_utc`, rows from 2026-10-20 on).

A day is archived only once its close is in the file (as-of at or after 15:59 New York) and only after the file has passed every check: at least half of its contracts quoted two-sided (a pre-open ETF file can carry quotes on 1% of contracts), and a successful pricing. Nothing is written before that. An archived close is never overwritten unless asked (`archive --force`), so the morning catch-up only fills missing days. Before the open the ETF files carry quotes on about 1% of their contracts, so the catch-up can recover a missed evening for the indices only. `python cli.py rebuild` (`study.rebuild_history`) recomputes the whole table from the raw files after any change to the engine; a file that fails the checks, or a row without a raw file, keeps its existing row and is reported.

**Dataset.** For each symbol, the features at the close of $t$ are paired with outcomes on $t+1$.

| Features at $t$ | Outcomes on $t+1$ |
|---|---|
| Regime sign | Realized vol, Garman-Klass |
| Net GEX z-score (expanding mean and std up to $t-1$, so no look-ahead) | Realized vol, close-to-close |
| Zero-gamma distance in one-day sigmas | High-low range in % of the previous close |
| Front share of $|GEX|$ | Return |
| One-day ATM implied vol (control) | Touch and hold of the call and put walls of $t$ |
| Today's realized vol (control) | |

Realized vols are annualized with 252 trading days.

Two consistency checks guard the dataset:
* a pair is kept only if $t$ and $t+1$ are consecutive sessions: adjacent business days, or, across a holiday, $t+1$'s close minus its price change equals the archived close of $t$ (to 0.01%). A missing archive day therefore never glues two non-consecutive sessions. Returns and ranges of $t+1$ are measured from the ARCHIVED close of $t$. The vendor's `prev_day_close` cannot be used: in closing files it equals the day's own close (SPX 2026-10-12: 7,807.24 vs 7,807.2402, while the day's change was −4.30). For ETFs, the price change follows the after-hours price, so the holiday bridge only works for indices; ETF pairs around a holiday are dropped;
* an OHLC row is used for open-based measures only if low ≤ open, close ≤ high. The vendor's NDX open on 2026-10-12 is above the high.

**Statistics.** Each needs at least 30 pairs.
* Conditional means by regime, with Welch t-statistics.
* OLS of each outcome on the features with Newey-West standard errors: Bartlett kernel, $L = \lfloor 4 (n/100)^{2/9} \rfloor$, implemented in numpy. A feature that is constant over the sample is dropped and named.
* Touch and hold frequencies of the walls.

The tests check:
* the HAC formula against a direct double sum, and against White HC0 when $L = 0$;
* that a simulated 3-point regime effect is recovered and that its absence is not "found";
* the no-look-ahead property of the z-score;
* the consecutive-day rule.

## 11. Validation (fixture: CBOE files of the 2026-10-12 close)

| Check | SPX | SPY |
|---|---|---|
| Our gamma / CBOE gamma, liquid ±5%, median [p5, p95] | 1.000 [0.94, 1.06] | 0.98 [0.81, 1.01] |
| Our IV − CBOE IV, vol pts, median | −0.15 | +0.11 |
| Implied spot − vendor spot | −5.4 bp | +3.7 bp |
| Open interest dropped as settled | 1.6% | 3.0% |
| Net GEX, all expiries, $bn per 1% | +83.2 | +3.9 |
| Zero gamma | 7,752.4 (−0.65%) | 776.39 (−0.21%) |

The SPX gamma band is the width of CBOE's $10^{-4}$ rounding. NDX, RUT and XSP are checked the same way: parity forwards, implied spot within 20 bp of the vendor's, and AM roots settling 6.5 h before PM roots on the same date.

**Session check (files of 2026-10-13, generated 09:48 New York).**

| Check | SPX | NDX |
|---|---|---|
| Index print / latest option trade | 09:32:58 / 09:32:58 | 09:32:59 / 09:32:59 |
| Front expiry | 2026-10-13, 0DTE, 6.45 h to settlement | same |
| 0DTE gamma / CBOE gamma, ±1% of spot, median | 0.996 | 0.993 |
| Implied spot − vendor spot | −2.5 bp | 0.0 bp |
| Our IV − CBOE IV, vol pts, median | −0.02 | −0.02 |

In session, the index and the options are delayed by the same 15 minutes, so the as-of rule (section 1) is exact and the 0DTE clock matches CBOE's. The SPY file of 09:44 carried no two-sided quote at all: the delayed feed only shows the open about 15 minutes after it. The engine refuses such a file with an explicit message, and the app keeps showing the last good state. The two session files and the empty one are test fixtures.

Run the full test suite (offline) with `pytest`.

## 12. Known limits

* **Open interest is published once a day** (OCC, before the open). Intraday 0DTE positioning is invisible to any OI-based GEX. The Flow page (section 9) addresses it given a trade file; without one, the structural view is all there is.
* **The dealer convention is an assumption.** It is reasonable for index options, but fails where customers buy calls (single stocks, squeezes).
* **European pricing on American ETF options** (SPY, QQQ, IWM). Vols are read from out-of-the-money prices, where the early-exercise premium is negligible. Deep in-the-money puts carry negligible gamma.
* **Sticky strike, frozen time.** The profile answers "this book re-marked at another spot now", not "the book after the market gets there". The band shows how much the sticky-moneyness alternative moves the zero gamma.
* **The archived close carries the open interest of the START of its session.** CBOE republishes the file before the next open with the open interest updated overnight by the OCC (positions at the END of the session), the quotes unchanged: on 2026-10-19 the SPX net GEX was −27.7bn with the evening file and −35.0bn with the morning one. The archive keeps the evening convention; a pre-open snapshot, fresher for forecasting the next session, is a candidate addition. Known exception: the raw files of 2026-10-19 for SPX, NDX, RUT, XSP, QQQ and IWM were overwritten by their morning versions before the no-overwrite rule existed, while their history rows come from the evening files; a `rebuild` would switch those six rows to the morning open interest. The SPY evening file of that day was lost (replaced by a pre-open file, set aside as `2026-10-19_preopen-corrupt.json.gz` on the author's machine, not in the repository); its history row is intact, and `rebuild` keeps it.
* **Holidays are not modelled** in the "next close" scenario or in the frozen-source flag.
* **Early-close sessions** (13:00 New York): the 15:59 rule archives nothing those days. One pair is lost per early close.
* **A constant $r$ (4%) is used for discounting only**; its effect on gamma is second order because the vol is implied with the same $DF$.
* **CBOE delayed data has no SLA and no history.** Production needs a licensed feed returning the `Chain` schema of `gex_engine/data.py`.

## 13. Strategy backtest (pre-registered)

**Question.** Does trading the gamma regime pay, net of costs, out of sample, better than the same rules without the regime and better than a plain VIX filter? Everything below was fixed and committed before the first run: rules, regimes, benchmarks, execution and inference in commit `d7c2974` (2026-10-15 13:11 CEST), first results in `df8f8f1` (13:16), the HAR controls, two-sided placebo and reversal alignment in `0704d5b` (13:21). These commits are in the author's development history; this repository starts from a single later commit. Any later change is a new trial and is logged in `data/backtest/trials.csv`.

**Data.**

| Input | Source | Note |
|---|---|---|
| GEX regime | SqueezeMetrics public SPX GEX, daily since 2011 (`data/external/`) | their methodology, not ours: on our 3 archived days the two disagree in sign once (2026-10-14). The backtest therefore tests the market's GEX, not `gex_engine`. `backtest.gex_series(source="own")` swaps in our history when it is long enough |
| ES | SPXT total return minus the 3-month US yield (act/360), Bloomberg daily closes (`data/market/prices.csv`, licensed, not in the repository; its provenance file is) | excess return of a fully collateralized future; roll and basis ignored |
| UX1 | front VIX future, Bloomberg generic, chained across rolls | the generic rolls on the expiry session up to Nov-2014, the session after from Dec-2014 (identified on the data: the curve UX1..UX3 matches yesterday's UX2..UX4) |
| Controls | VIX close; Parkinson vol from SPX high and low | the SPX open is stale before 2016 (equal to the previous close on 30% of 2012 sessions) and is not used |

Sample: the sessions where every series exists, 2012-01-04 to 2026-09-18 (the Bloomberg export day is dropped: intraday values). SqueezeMetrics' price column matches the Bloomberg close of the same session on 3,861 of 3,870 days (the rest within 0.04%): the GEX is dated by its session, not shifted.

**Regimes** (known at the close of t), the trials:

| Regime | −1 (negative gamma) when |
|---|---|
| `gex_sign` | GEX < 0 |
| `gex_low` | GEX below the 25th percentile of its own past (expanding, ≥ 250 sessions, threshold at t−1) |

GEX is negative on only 9% of sessions (41% in 2022, 0% in 2024), so `gex_sign` hardly differs from always-positive out of sample; `gex_low` is the variant that keeps two regimes in every period. The number of regime **episodes**, not days, is reported: a few long episodes are a few independent observations.

**Rules.**

| Rule | Asset | Position decided at the close of t |
|---|---|---|
| reversal | ES | −r_t × sign(ES return of t): fade the day in positive gamma, follow it in negative gamma |
| exposure | ES | r_t: long in positive gamma, short in negative gamma |
| vol | UX1 | −r_t: short the VIX future in positive gamma, long in negative gamma |

**Benchmarks** (not trials): each rule with r_t = +1 always (does the regime add anything?) and with a VIX filter, r_t = −1 when VIX is above its expanding median (does GEX add anything beyond the level of volatility, with which it is correlated: 0.33 between "GEX > 0" and "VIX below median").

**Execution and sizing.**
* The position decided at the close of t earns the return of session t + lag. **lag 2** (primary): entered at the close of t+1, robust to the unknown publication time of the GEX. **lag 1**: entered right after the close of t, an optimistic bound (ES trades until 17:00 New York). An edge that exists only at lag 1 is a timing artefact.
* Weight = 10% / trailing 60-session annualized vol of the asset (known at t), capped at 2. Without it, short UX1 at 1× loses 113% on 2018-02-05.
* Costs per unit of notional traded, per side: ES 1 bp; VIX futures 0.05 point over the price. Also shown ×2.
* Daily excess returns, summed (constant capital).

**Inference.**
* In-sample 2012-01-04 to 2023-12-29; out-of-sample from 2024-01-01, looked at once.
* Sharpe with its standard error (Lo 2002) and a circular block bootstrap interval (blocks of 20 sessions). Over ~3,700 sessions the standard error is about 0.26: a Sharpe below ~0.5 cannot be told from zero.
* Newey-West t-statistic of the mean daily return.
* Deflated Sharpe ratio (Bailey and López de Prado 2014), N = number of trials ever run (`data/backtest/trials.csv`, append-only: 3 rules × 2 regimes × 2 lags = 12 at the first run).
* Placebo: the regime series shifted circularly by at least its longest episode (500 draws, fixed seed); p-value = share of placebos with a Sharpe at least the actual one.
* Mechanism: OLS of next-session Parkinson vol on the negative-gamma dummy, VIX and today's vol (Newey-West). The reversal and vol rules rely on this effect; without it, their P&L has no reason to exist.

### 13.1 Results (2026-10-15, data to 2026-09-18)

Two corrections were made after the first run, both before reading any new result, and both are logged:
* **reversal** faded the return of t; at lag 2 that is a move two sessions old, not what the thesis says. It now fades or follows the last session before entry. The corrected rule is 4 new trials (`RULE_VERSION`), so N = 16.
* **The mechanism regression** gained the standard volatility controls: HAR terms (5- and 22-session mean vol: realized vol has long memory) and leverage terms (today's return, down-day dummy: negative-gamma sessions fall in sell-offs), with 20 Newey-West lags. This is a robustness check, not a trial.

Primary execution (close t+1), costs ×1, full sample 2012-2026, net Sharpe [5%, 95% bootstrap]:

| Rule | gex_sign | gex_low | always (benchmark) | VIX filter (benchmark) |
|---|---|---|---|---|
| reversal | **−0.57** [−1.01, −0.13] | **−0.49** [−0.90, −0.09] | −0.08 | −0.40 |
| exposure | 0.35 [−0.07, 0.79] | 0.10 [−0.33, 0.55] | **0.76** | −0.31 |
| vol | 0.05 [−0.37, 0.53] | −0.36 [−0.75, 0.11] | **0.34** | −0.66 |

* **No rule beats its always-on benchmark.** Deflated Sharpe ratios (N = 16) are between 0% and 10%, against a usual bar of 95%. At the optimistic execution (close t), `exposure | gex_sign` rises to 0.57 but stays below always-long (0.79).
* **The regime timing IS informative, in the opposite direction to the rules.** The two-sided placebo (regime shifted in time) puts the actual Sharpe below 96-100% of the shifted ones for reversal (p_worse 0.000 and 0.038), exposure | gex_sign (0.022) and vol | gex_low (0.050). After a negative-gamma close, the next session tends to REVERSE the move and the market to rebound. Following the move, going short, or buying vol then all lose. The deep-search report's thesis ("negative gamma = momentum") is rejected at the daily horizon on this data. Intraday momentum is not tested.
* **A rule that takes the other side would be post-hoc.** It was chosen after seeing these results. It would have to be registered as a new trial, and the out-of-sample period can't confirm it (19 negative-GEX sessions in 11 episodes since 2024). It is not reported as profitable.
* **The vol mechanism is robust.** Next-session Parkinson vol after a negative-GEX close: +2.7 vol points (t = 4.8) with VIX and today's vol; **+2.1 (t = 4.1)** after adding HAR and leverage controls. After a low-gamma close: +1.9 (t = 6.1), then **+1.4 (t = 5.0)**. The VIX-filter dummy adds nothing (−0.4, t = −1.3). GEX carries information about next-day realized volatility that the level and memory of volatility do not. These rules do not monetize it.
* The reversal rule turns over about 210× its notional a year: costs alone take about 2.1%/yr.

**Conclusion.** As a directional or vol-carry timing rule, the market's GEX regime is not profitable net of costs once compared with doing the same thing all the time. As a predictor of next-day realized volatility, it is significant and robust to the usual controls.

**Limits of this run.**
* The regime is SqueezeMetrics' GEX, not `gex_engine`'s.
* UX1 roll costs are not charged when the position does not change through a roll (about 12 a year, roughly 0.05-0.1 of Sharpe). This affects the benchmark as much as the trials, so the ranking holds.
* GEX in dollars is not stationary: it grows with S² and with open interest, so the expanding 25th percentile makes `gex_low` rarer over time (171 episodes in-sample, 16 out-of-sample).

**Candidate next trials** (each to be approved, registered and logged before running):
1. short vol sized down in negative gamma instead of flipped;
2. a variance-risk-premium rule using a GEX-augmented realized-vol forecast;
3. a normalized GEX (GEX / S², or a 1-year rolling quantile);
4. the post-hoc reversed rules, explicitly labelled as such.
