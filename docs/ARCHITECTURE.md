# NSE Bhavcopy Screener - What the code does

End-of-day (EOD) pipeline that downloads NSE UDiFF CM Bhavcopy, filters the Nifty 200 universe for quality,
detects breakout / pullback setups, and builds a risk-sized plan within a small INR budget.
Everything is driven by `config/settings.yaml`; nothing in the code needs editing to change a rule.

Run from `src/`:

```
python main.py                                    # whole pipeline
python -m bhav_screener.pipeline --from layer2    # restart at a step
python -m bhav_screener.pipeline --only signals   # run one step
```

Steps: `download -> universe -> layer2 -> signals -> allocator`.

---

## 1. Big picture

```mermaid
flowchart TD
    CFG["config/settings.yaml<br/>every rule and path"]
    NSE[("NSE archive<br/>UDiFF CM Bhavcopy zip")]
    IDX[("niftyindices.com<br/>Nifty 200 constituents CSV")]
    UNIV["config/universe_nifty200.csv<br/>symbol, industry"]

    subgraph L1["Layer 1 - download and store"]
        DL["NseBhavcopyDownloader<br/>BhavcopyBackfill + BhavcopyFileParser"]
        CACHE[("data/cache/bhavcopy-cache.json")]
        P1[("data/results/layer1/<br/>one parquet per trading day")]
    end

    subgraph L2["Layer 2 - quality filter"]
        F2["Layer2Filter + corporate_actions"]
        P2A[("layer2_adjusted_daily.parquet<br/>full history + indicators")]
        P2Q[("layer2_qualified_daily.parquet<br/>rows that pass the gates")]
    end

    subgraph L3["Layer 3 - signals"]
        F3["SignalScreener (ta library)"]
        P3[("layer3_candidates.parquet<br/>layer3_candidates_DATE.csv")]
    end

    subgraph L4["Layer 4 - risk allocator"]
        F4["RiskAllocator"]
        P4[("screener_plan_DATE.json<br/>screener_report_DATE.md")]
    end

    ST[("data/status/*.json<br/>one status file per step")]

    NSE --> DL
    DL <--> CACHE
    DL --> P1
    IDX --> UNIV
    UNIV --> DL
    UNIV --> F2
    UNIV --> F4
    P1 --> F2
    F2 --> P2A
    F2 --> P2Q
    P2A --> F3
    P2Q --> F3
    F3 --> P3
    P3 --> F4
    F4 --> P4
    CFG -.-> DL
    CFG -.-> F2
    CFG -.-> F3
    CFG -.-> F4
    DL -.-> ST
    F2 -.-> ST
    F3 -.-> ST
    F4 -.-> ST
```

Rule: each step writes its result to a file and the next step reads that file (not memory), so any step can run alone.

---

## 2. Pipeline control flow

```mermaid
flowchart TD
    A["main.py -> pipeline.main()"] --> B["parse --from / --only"]
    B --> C{"--only given?"}
    C -- "yes" --> D["steps = [that step]"]
    C -- "no" --> E["steps = STEPS from --from onward"]
    D --> F
    E --> F["next step"]
    F --> G["run step action"]
    G --> H{"raised an error?"}
    H -- "no" --> I["record ok + summary + seconds"]
    I --> J{"more steps?"}
    J -- "yes" --> F
    J -- "no" --> K["outcome = ok"]
    H -- "yes" --> L["record failed + error"]
    L --> M["outcome = failed, stop here"]
    K --> N["write data/status/pipeline_status.json"]
    M --> N
    N --> O{"outcome ok?"}
    O -- "yes" --> P["exit code 0"]
    O -- "no" --> Q["exit code 1"]
```

`layer2`, `signals` and `allocator` also write their own `failed` status (with the error) before re-raising.

---

## 3. Layer 1 - download, parse, cache

### 3.1 Backfill loop

```mermaid
flowchart TD
    A["BhavcopyBackfill.run()"] --> B["NiftyUniverse() created first<br/>refreshes universe file if stale"]
    B --> C["days_back = ceil(history_trading_days x 365/252) + 10<br/>end = today"]
    C --> D["for each calendar day, oldest first"]
    D --> E{"Saturday or Sunday?"}
    E -- "yes" --> D
    E -- "no" --> F{"parquet already on disk<br/>but not in cache?"}
    F -- "yes" --> G["adopt: add to cache as saved"]
    F -- "no" --> H{"cache says saved or holiday?"}
    G --> Z1["stats.cached += 1"]
    H -- "yes" --> Z1
    H -- "no" --> I["_fetch_one(day)"]
    I --> J{"exception?"}
    J -- "yes" --> K["stats.failed += 1<br/>NOT cached, retried next run"]
    J -- "no" --> L["sleep request_delay_seconds"]
    K --> L
    Z1 --> D
    L --> D
    D -- "done" --> M["finally: save cache file"]
    M --> N["write layer1_backfill status<br/>ok if no failures, else partial"]
```

### 3.2 One day

```mermaid
flowchart TD
    A["downloader.download(day)"] --> B["GET url_template with date"]
    B --> C{"HTTP status"}
    C -- "404" --> D["return None"]
    C -- "other error" --> E["raise -> counted as failed"]
    C -- "200" --> F["save zip to results/layer1/raw"]
    D --> G["stats.holidays += 1"]
    G --> H{"within recent_days of today?"}
    H -- "yes" --> I["NOT cached<br/>NSE may not have published yet"]
    H -- "no" --> J["cache.mark_holiday"]
    F --> K["BhavcopyFileParser.parse_udiff_cm<br/>expected_date, eq_only"]
    K --> L{"keep_raw?"}
    L -- "no" --> M["delete the zip"]
    L -- "yes" --> N["keep the zip"]
    M --> O
    N --> O{"dataframe empty?<br/>(bad file or trade date mismatch)"}
    O -- "yes" --> P["raise -> counted as failed"]
    O -- "no" --> Q["keep STORED_COLUMNS only<br/>write zstd parquet"]
    Q --> R["cache.mark_saved(rows)<br/>stats.saved += 1"]
```

Stored columns: `trade_date, symbol, series, open, high, low, close, prev_close, volume, turnover_cr`.

### 3.3 Cache file

```json
{"2026-11-20": {"status": "saved", "rows": 2760},
 "2026-11-14": {"status": "holiday"}}
```

A date counts as done when it is a holiday, or saved **and** its parquet still exists. If the parquet is deleted, the date is fetched again.

---

## 4. Universe (Nifty 200)

```mermaid
flowchart TD
    A["NiftyUniverse() created<br/>by Backfill, Layer2, Allocator, pipeline"] --> B{"universe_file set?"}
    B -- "no (empty string)" --> Z["nothing: use all EQ symbols"]
    B -- "yes" --> C{"file missing<br/>or older than refresh_days (7)?"}
    C -- "no" --> OK["use existing file"]
    C -- "yes" --> D["download index_url CSV"]
    D --> E{"download and parse OK?<br/>symbol column found,<br/>at least min_index_symbols (150)"}
    E -- "yes" --> F["write config/universe_nifty200.csv<br/>symbol, industry"]
    E -- "no" --> G{"old file exists?"}
    G -- "yes" --> H["warn, keep old file"]
    G -- "no" --> I{"fallback symbols given?"}
    I -- "yes" --> J["write fallback with industry Unknown"]
    I -- "no" --> K["raise RuntimeError"]
```

- `symbols()` = CSV symbols plus any listed under `universe.symbols` in settings; empty means all.
- `sector_map()` = symbol to industry; used by the allocator for the sector cap (`Unknown` if missing).
- Rows starting with `NIFTY`, blanks and duplicates are removed when parsing.

---

## 5. Layer 2 - quality filter

```mermaid
flowchart TD
    A["Layer2Filter.run()"] --> B["read every layer1 parquet<br/>filter series == EQ and symbol in universe"]
    B --> C{"any rows?"}
    C -- "no" --> X["raise: run the backfill first<br/>status failed"]
    C -- "yes" --> D["adjust_for_corporate_actions"]
    D --> E["add indicators per symbol"]
    E --> F["quality mask"]
    F --> G["save adjusted parquet (all rows)"]
    F --> H["save qualified parquet (rows passing)"]
    G --> I["status: ok if any symbol qualifies on latest day, else empty"]
    H --> I
```

### 5.1 Corporate-action adjustment

```mermaid
flowchart TD
    A["for each symbol, sorted by date"] --> B["ratio = prev_close / previous row close"]
    B --> C{"abs(ratio - 1) > corporate_action_threshold (5%)?"}
    C -- "no" --> D["event = 1<br/>dividend, rounding: ignored"]
    C -- "yes" --> E["event = ratio<br/>split or bonus"]
    D --> F["factor of a row = product of events AFTER it"]
    E --> F
    F --> G["open, high, low, close, prev_close x factor<br/>volume / factor"]
    G --> H["column ca_factor keeps the factor<br/>1.0 = unadjusted"]
```

### 5.2 Indicators and gates

| Indicator | Definition |
|---|---|
| `sma_20`, `sma_50`, `sma_200` | rolling mean of close |
| `avg_volume` | 20-day mean volume |
| `adtv_cr` | 20-day mean `turnover_cr` |
| `atr` | true range, exponential mean, alpha = 1/14, needs 14 rows |
| `atr_pct` | `atr / close x 100` |

```mermaid
flowchart TD
    A["row with indicators"] --> B{"close >= min_close (50)?"}
    B -- "no" --> X["excluded"]
    B -- "yes" --> C{"max_close set and close <= max_close?"}
    C -- "fails" --> X
    C -- "ok" --> D{"avg_volume >= min_avg_volume (200,000)?"}
    D -- "no" --> X
    D -- "yes" --> E{"adtv_cr >= min_avg_turnover / 1 crore (2 Cr)?"}
    E -- "no" --> X
    E -- "yes" --> F{"atr_pct between 1.5 and 6.0?"}
    F -- "no" --> X
    F -- "yes" --> G{"close above every SMA in<br/>require_close_above_smas (50, 200)?"}
    G -- "no" --> X
    G -- "yes" --> Q["qualified"]
```

A missing indicator (not enough history) is treated as a fail, so the 200-day SMA needs at least 200 rows per symbol.

---

## 6. Layer 3 - signals

### 6.1 Overall

```mermaid
flowchart TD
    A["SignalScreener.run()"] --> B{"adjusted and qualified parquet exist?"}
    B -- "no" --> X["raise: run Layer2Filter first<br/>status failed"]
    B -- "yes" --> C["as_of = latest date in history"]
    C --> D["eligible = symbols qualified on as_of"]
    D --> E["compute features on full history (ta library)"]
    E --> F["keep rows: as_of date AND eligible"]
    F --> G["detect setups"]
    G --> H["stop, target, validation"]
    H --> I["save parquet + CSV (sorted by RS score)"]
    I --> J["status: ok if candidates, else empty"]
```

### 6.2 Features

| Feature | How |
|---|---|
| `rsi` | `ta` RSIIndicator, 14 |
| `breakout_high` / `prior_high` | Donchian high over 20 days, shifted 1 day (today excluded) |
| `recent_high` | Donchian high over 10 days (pullback depth) |
| `swing_low_breakout` | Donchian low over 7 days |
| `swing_low_pullback` | Donchian low over 10 days |
| `high_52w` | Donchian high over 252 days (uses available history) |
| `volume_ratio` | volume / mean of the previous 20 days |
| `pct_below_52w_high` | `(high_52w - close) / high_52w x 100` |
| `rs_score` | 126-day return, percentile rank (0-100) across all symbols on the same date |

### 6.3 Setup detection

```mermaid
flowchart TD
    A["eligible symbol on as_of"] --> B{"BREAKOUT<br/>close > prior_high<br/>AND volume_ratio >= 1.5<br/>AND rsi 55 to 80"}
    B -- "yes, and breakout enabled" --> S1["setup = breakout"]
    B -- "no" --> C{"PULLBACK<br/>drop from 10-day high >= 3%<br/>AND low <= SMA20 x 1.02<br/>AND close >= SMA20<br/>AND close > previous close"}
    C -- "yes, and pullback enabled" --> S2["setup = pullback<br/>(no volume rule)"]
    C -- "no" --> X["no setup"]
    S1 --> D
    S2 --> D{"pct_below_52w_high <= 15<br/>AND rs_score >= min_rs_score?"}
    D -- "no" --> X
    D -- "yes" --> E["go to stop and target"]
```

Breakout wins if both match. `scanner.setups` can turn either one off.

### 6.4 Stop, target and validation (two modes)

```mermaid
flowchart TD
    A["candidate with setup<br/>entry = last close"] --> B{"intraday.enabled?"}
    B -- "true (current default)" --> C["stop = entry - stop_atr (0.6) x ATR<br/>reward_risk = 1.5<br/>max stop % = intraday.max_stop_pct (3.0)"]
    B -- "false (positional)" --> D{"setup type"}
    D -- "breakout" --> E["swing low = lowest low of last 7 days"]
    D -- "pullback" --> F["swing low = lowest low of last 10 days"]
    E --> G["stop = swing low - stop_atr_buffer (0.15) x ATR"]
    F --> G
    G --> H["stop = min(stop, entry - min_stop_atr (0.5) x ATR)<br/>keeps the stop outside daily noise"]
    H --> I["reward_risk = risk.reward_risk (2.0)<br/>max stop % = scanner.max_stop_pct (6.5)"]
    C --> J["risk_per_share = entry - stop<br/>risk_pct = risk_per_share / entry<br/>target = entry + reward_risk x risk_per_share"]
    I --> J
    J --> K{"risk_per_share > 0<br/>AND risk_pct <= max stop %?"}
    K -- "no" --> X["skipped, never tightened"]
    K -- "yes" --> L{"intraday mode?"}
    L -- "no" --> Y["candidate kept"]
    L -- "yes" --> M{"target distance <= max_target_atr (1.2) x ATR?"}
    M -- "no" --> X
    M -- "yes" --> Y
```

Candidate columns: `trade_date, symbol, setup, entry, stop, risk_per_share, risk_pct, target, rs_score, rsi, volume_ratio, atr_pct, pct_below_52w_high, adtv_cr`.

---

## 7. Layer 4 - risk allocator

```mermaid
flowchart TD
    A["RiskAllocator.run()"] --> B{"layer3_candidates.parquet exists?"}
    B -- "no" --> X["raise: run SignalScreener first<br/>status failed"]
    B -- "yes" --> C{"rank_by"}
    C -- "stop_pct" --> D["sort risk_pct ascending<br/>RS score descending breaks ties"]
    C -- "rs_score" --> E["sort RS score descending"]
    D --> F
    E --> F["for each candidate in order"]
    F --> G["qty = min of<br/>floor(max_risk_per_trade / risk_per_share)<br/>floor(max_position_value / entry)<br/>floor(capital left / entry)"]
    G --> H{"selected count >= max_positions?"}
    H -- "yes" --> R1["rejected: max_positions reached"]
    H -- "no" --> I{"sector count >= max_per_sector?"}
    I -- "yes" --> R2["rejected: sector cap reached"]
    I -- "no" --> J{"qty < 1?"}
    J -- "yes" --> R3["rejected: one share exceeds per-trade risk,<br/>position size or remaining capital"]
    J -- "no" --> K{"daily risk + trade risk > max_daily_risk?"}
    K -- "yes" --> R4["rejected: daily risk cap would be exceeded"]
    K -- "no" --> S["selected<br/>add to daily risk, capital used, sector count"]
    R1 --> F
    R2 --> F
    R3 --> F
    R4 --> F
    S --> F
    F -- "done" --> P["write plan JSON + report MD"]
    P --> Q["status: ok if any selected, else empty"]
```

- `max_position_value = capital x max_position_pct / 100`.
- Rejections are checked in the order above, so the reason shown is the first rule that failed.
- In intraday mode every selected trade also carries `exit_policy: EOD_MANDATORY_CLOSE` and `square_off_time` (default `15:15`). This is a tag only; the project does not place or close orders.
- With no candidates the plan file is named `screener_plan_none.json`.

Plan JSON: `as_of_date`, `limits` (including `capital_inr`, `rank_by`), `summary` (`candidates_in`, `selected`, `total_risk_inr`, `total_position_value_inr`, `capital_left_inr`), `selected[]`, `rejected[]`.

---

## 8. Data folder

```mermaid
flowchart LR
    D["data/"] --> C["cache/<br/>bhavcopy-cache.json"]
    D --> S["status/<br/>layer1_backfill, layer2, layer3,<br/>layer4, pipeline  (_status.json)"]
    D --> R["results/"]
    R --> R1["layer1/<br/>DATE.parquet x trading days<br/>raw/ (zips, only if keep_raw)"]
    R --> R2["layer2/<br/>layer2_adjusted_daily.parquet<br/>layer2_qualified_daily.parquet"]
    R --> R3["layer3/<br/>layer3_candidates.parquet<br/>layer3_candidates_DATE.csv"]
    R --> R4["layer4/<br/>screener_plan_DATE.json<br/>screener_report_DATE.md"]
```

`data/` is git-ignored and can be deleted at any time; the next run rebuilds it.

### Status files

```mermaid
stateDiagram-v2
    [*] --> ok: step finished with output
    [*] --> empty: finished, nothing qualified or selected
    [*] --> partial: layer1 only, some days failed
    [*] --> failed: exception, error text saved
```

Each file is written atomically to `data/status/<layer>_status.json` with the run details (counts, file paths, symbols).

---

## 9. Code map

```
config/settings.yaml                      all rules
config/universe_nifty200.csv              auto-created, refreshed weekly
src/main.py                               calls pipeline.main()
src/bhav_screener/
  pipeline.py                             ScreenerPipeline, CLI
  nse_bhavcopy_downloader.py              HTTP download of one day (independent)
  bhavcopy_backfill.py                    Layer 1 loop, cache, parquet
  risk_allocator.py                       Layer 4
  filters/
    corporate_actions.py                  split / bonus back-adjustment
    layer2_filter.py                      Layer 2
    signals.py                            Layer 3
  utils/
    config.py                             typed settings, validation, PROJECT_ROOT
    bhavcopy_file_parser.py               UDiFF CSV/ZIP parser
    bhavcopy_cache.py                     cache file
    universe.py                           Nifty 200 file + sectors
    status.py                             write_status()
```

---

## 10. Config sections

| Section | Controls |
|---|---|
| `data` | folders, history window (trading days), data-age limit, download URL, user agent, timeout, delay, raw-file keeping, recent-day holiday rule |
| `universe` | EQ only, extra symbols, universe file, index URL, refresh days, corporate-action threshold, price / volume / turnover floors |
| `scanner` | SMA periods, breakout, volume, RSI, ATR band, 52-week distance, pullback, positional stop rules, RS settings |
| `risk` | reward/risk, ranking, per-trade and daily INR risk, capital, position size %, max positions, sector cap |
| `intraday` | on/off, ATR stop, reward/risk, target-vs-ATR limit, stop % cap, exit policy, square-off time |

Loading rules: missing keys use the defaults in `utils/config.py`; an unknown section or key raises an error; invalid
values (setups, `rank_by`, `square_off_time`, SMA lists, non-positive risk or intraday limits, history shorter than
the longest indicator window plus 5 days) raise an error at start.

---

## 11. Edge cases and limits

| Case | Behaviour |
|---|---|
| Weekend | skipped, no request |
| NSE holiday (404) | counted as holiday; cached unless within `recent_days` of today |
| Today's file not published yet | 404 inside `recent_days`, not cached, retried next run |
| Download or parse error for a day | counted as failed, not cached, layer1 status `partial` |
| Trade date inside file differs from expected | parser returns empty, treated as failed |
| Parquet deleted | cache entry ignored, day downloaded again |
| Universe download fails | keep old file; if none, use fallback, else error |
| Fewer than 150 symbols parsed | download rejected as bad |
| Split or bonus | prices and volume back-adjusted by the `prev_close` gap (more than 5%) |
| Symbol with under 200 rows | cannot pass the 200-day SMA gate |
| History window of exactly 200 days | works only if no trading day is missing; use 260 or more for margin |
| No candidates | files still written, status `empty`, plan has no trades |
| Stop too wide or target too far | candidate dropped, not adjusted |
| Price above per-position budget | rejected, one share exceeds the limit |
| Sector missing in the universe file | treated as sector `Unknown` |
| Delivery percentage | not in UDiFF bhavcopy, so only the volume check is applied |
| Entry price | last close, a reference only; recompute from live price before trading |
| Order placement | not part of this project |
