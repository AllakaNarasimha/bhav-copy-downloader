# bhav-copy-downloader

Automated NSE end-of-day screener. It downloads the NSE UDiFF cash-market Bhavcopy, keeps the Nifty 200 stocks that pass quality gates, detects breakout and pullback setups, and writes a risk-sized daily plan within strict INR limits.

The default config is set up for a small account (INR 10,000) and **intraday** trades: ATR-based stop and target, plus an end-of-day square-off tag. Switch `intraday.enabled` to `false` for positional trades with swing-low stops.

> Educational tool only. It does not place orders and is not investment advice. Entry is the last close, used as a reference.

Detailed diagrams for every case (holidays, cache, corporate actions, rejections) are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Quick start

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
cd src
..\.venv\Scripts\python main.py                           # whole pipeline
..\.venv\Scripts\python -m bhav_screener.pipeline --from layer2   # restart at a step
..\.venv\Scripts\python -m bhav_screener.pipeline --only signals  # run one step
```

Steps: `download`, `universe`, `layer2`, `signals`, `allocator`. The pipeline stops at the first failing step and exits with code 1.

All rules live in [config/settings.yaml](config/settings.yaml). Missing keys fall back to defaults in `utils/config.py`; unknown keys or invalid values raise an error at start.

## Pipeline

Each step saves its result to a file, and the next step reads that file instead of memory. Any step can run on its own.

```mermaid
flowchart LR
    NSE[("NSE archive<br/>UDiFF Bhavcopy")] --> L1["download<br/>Layer 1"]
    IDX[("niftyindices.com<br/>Nifty 200 list")] --> U["universe"]
    U --> CSV[("config/<br/>universe_nifty200.csv")]
    L1 --> R1[("results/layer1<br/>daily parquet")]
    R1 --> L2["layer2<br/>quality filter"]
    CSV --> L2
    L2 --> R2[("results/layer2<br/>adjusted + qualified")]
    R2 --> L3["signals<br/>Layer 3"]
    L3 --> R3[("results/layer3<br/>candidates")]
    R3 --> L4["allocator<br/>Layer 4"]
    L4 --> R4[("results/layer4<br/>plan JSON + report")]
```

### Layer 1: download

Fetches one Bhavcopy zip per trading day for the last `history_trading_days` (260) and stores a parquet file per day.

```mermaid
flowchart TD
    A["For each weekday in the window"] --> B{"Parquet exists<br/>or cache says done?"}
    B -- yes --> C["Skip (cached)"]
    B -- no --> D["GET zip from NSE"]
    D --> E{"HTTP status"}
    E -- "404" --> F{"Within recent_days<br/>of today?"}
    F -- yes --> G["Holiday, not cached<br/>(file may not be published yet)"]
    F -- no --> H["Cache as holiday"]
    E -- "other error" --> I["Count as failed<br/>(retried next run)"]
    E -- 200 --> J["Parse with BhavcopyFileParser"]
    J --> K{"Empty or date mismatch?"}
    K -- yes --> I
    K -- no --> L["Write parquet, cache as saved"]
```

A failed day makes the pipeline's download step fail, so Layer 2 never runs on partial data.

### Universe

```mermaid
flowchart TD
    A["NiftyUniverse created"] --> B{"CSV missing or older<br/>than refresh_days (7)?"}
    B -- no --> Z["Use existing file"]
    B -- yes --> C["Download Nifty 200 list"]
    C --> D{"Download OK and<br/>at least min_index_symbols?"}
    D -- yes --> E["Write symbol, industry CSV"]
    D -- no --> F{"Old file exists?"}
    F -- yes --> G["Warn and keep old file"]
    F -- no --> H["Error: cannot create universe"]
```

### Layer 2: quality filter

```mermaid
flowchart TD
    A["Read layer1 parquet<br/>EQ series, universe symbols"] --> B["Back-adjust splits and bonus<br/>(prev_close vs earlier close gap above 5%)"]
    B --> C{"Over 30% of symbols mismatch<br/>on one date?"}
    C -- yes --> D["Treat as missing day,<br/>no adjustment"]
    C -- no --> E["Apply factor to earlier rows"]
    D --> F["SMA 20/50/200, ADTV, ATR, ATR%"]
    E --> F
    F --> G{"Close at least 50,<br/>avg volume at least 200k,<br/>turnover at least 2 Cr,<br/>ATR% in 1.5 to 6,<br/>close above SMA50 and SMA200"}
    G -- no --> H["Excluded"]
    G -- yes --> I[("layer2_qualified_daily.parquet")]
    F --> J[("layer2_adjusted_daily.parquet<br/>full history for Layer 3")]
```

### Layer 3: signals

Only symbols qualified on the latest date are evaluated. The run fails if the newest stored day is older than `data.max_data_age_days` (4).

```mermaid
flowchart TD
    A["Latest day, qualified symbols"] --> B["Features: RSI, Donchian highs and lows,<br/>volume ratio, 52-week distance, RS score"]
    B --> C{"Breakout?<br/>close above 20-day high,<br/>volume at least 1.5x,<br/>RSI 55 to 80"}
    C -- yes --> S["Setup = breakout"]
    C -- no --> D{"Pullback?<br/>down 3%+ from 10-day high,<br/>touched SMA20, closed above it,<br/>up on the day"}
    D -- yes --> S2["Setup = pullback"]
    D -- no --> X["No setup"]
    S --> E{"Within 15% of 52-week high<br/>and RS score ok?"}
    S2 --> E
    E -- no --> X
    E -- yes --> F{"intraday.enabled?"}
    F -- yes --> G["Stop = entry - 0.6 x ATR<br/>Target = entry + 1.5 x risk"]
    F -- no --> H["Stop = swing low - 0.15 x ATR,<br/>at least 0.5 x ATR below entry"]
    G --> I{"Stop within max_stop_pct<br/>(and target within 1.2 x ATR<br/>for intraday)?"}
    H --> I
    I -- no --> X
    I -- yes --> J[("layer3_candidates.parquet + CSV")]
```

### Layer 4: risk allocator

```mermaid
flowchart TD
    A["Read candidates"] --> B["Rank: tightest stop first,<br/>RS score breaks ties"]
    B --> C["Next candidate"]
    C --> D["qty = min(per-trade risk / risk per share,<br/>position cap, free capital / entry)"]
    D --> E{"Checks, in order"}
    E --> F["max_positions reached?"]
    E --> G["sector cap reached?"]
    E --> H["qty below 1?"]
    E --> I["daily risk cap exceeded?"]
    F & G & H & I -- any fails --> R["Rejected with reason"]
    F & G & H & I -- all pass --> S["Selected<br/>(intraday: exit policy + square-off time)"]
    R --> C
    S --> C
    S --> O[("screener_plan_DATE.json<br/>screener_report_DATE.md")]
```

## Output layout

`data/` is git-ignored and recreated by the pipeline.

```text
data/
  cache/bhavcopy-cache.json     which dates are saved or holidays
  status/<layer>_status.json    result of each step (ok, empty, failed)
  results/
    layer1/                     YYYY-MM-DD.parquet per trading day (raw/ holds zips if keep_raw)
    layer2/                     layer2_adjusted_daily.parquet, layer2_qualified_daily.parquet
    layer3/                     layer3_candidates.parquet, layer3_candidates_DATE.csv
    layer4/                     screener_plan_DATE.json, screener_report_DATE.md
```

## Files

| File | Purpose |
|---|---|
| `src/main.py` | Entry point; calls `pipeline.main()` |
| `src/bhav_screener/pipeline.py` | Runs the steps in order, CLI (`--from`, `--only`), writes `pipeline_status.json` |
| `src/bhav_screener/nse_bhavcopy_downloader.py` | Downloads one day's zip from NSE |
| `src/bhav_screener/bhavcopy_backfill.py` | Layer 1: loops over days, parses, writes parquet, uses the cache |
| `src/bhav_screener/filters/corporate_actions.py` | Split and bonus back-adjustment |
| `src/bhav_screener/filters/layer2_filter.py` | Layer 2: indicators and quality gates |
| `src/bhav_screener/filters/signals.py` | Layer 3: breakout and pullback detection, stops, targets |
| `src/bhav_screener/risk_allocator.py` | Layer 4: ranking, sizing, caps, plan and report |
| `src/bhav_screener/utils/config.py` | Typed settings, validation, `layer_path()` |
| `src/bhav_screener/utils/universe.py` | Nifty 200 CSV (`symbol`, `industry`) download and refresh |
| `src/bhav_screener/utils/bhavcopy_file_parser.py` | UDiFF CSV and zip parser |
| `src/bhav_screener/utils/bhavcopy_cache.py` | Saved and holiday date cache |
| `src/bhav_screener/utils/status.py` | Atomic status JSON writer |
| `config/settings.yaml` | All rules and paths |
| `config/universe_nifty200.csv` | Generated universe, refreshed weekly |
| `docs/ARCHITECTURE.md` | Detailed diagrams and edge cases |

## Configuration

Main sections of `config/settings.yaml`:

| Section | Controls |
|---|---|
| `data` | folders, history window, data-age limit, download URL, delay, raw-file keeping |
| `universe` | EQ only, universe file and refresh, corporate-action threshold, price, volume and turnover floors |
| `scanner` | SMAs, breakout, volume, RSI, ATR band, 52-week distance, pullback, positional stop rules, RS |
| `risk` | capital, per-trade and daily INR risk, position size %, max positions, sector cap, ranking |
| `intraday` | on/off, ATR stop, reward/risk, target limit, stop % cap, exit policy, square-off time |

## Limitations

- Delivery percentage is not in the UDiFF file, so only the volume check is applied.
- Sunday special sessions are not downloaded (the backfill skips weekends).
- No order placement; the square-off time is a tag in the plan.
- No automated tests yet.
