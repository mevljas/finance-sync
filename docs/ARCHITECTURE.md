# System Architecture (`finance-sync`)

## 1. High-Level Topology

```mermaid
flowchart LR
    subgraph Brokers["External Data Sources"]
        IBKR["IBKR Flex Web Service\n(HTTPS XML)"]
        TR["Trade Republic API\n(pytr WebSocket)"]
        DISCORD["Discord REST API v10\n(2FA Channel)"]
    end

    subgraph Homelab["Docker Compose / Portainer Stack"]
        IW["ibkr_worker\n(Polling: 6h)"]
        TW["tr_worker\n(Polling: 15m)"]
        subgraph Volume["/app/data (Shared Persistent Volume)"]
            SQLITE[("finance_sync.db\nSQLite WAL Mode")]
            TRSESS[("TR Session & Cookies")]
        end
    end

    subgraph Target["BudgetBakers Wallet API"]
        BB_REC["POST / DELETE / GET\n/v1/api/records"]
        BB_ACC["GET / PATCH\n/v1/api/accounts"]
    end

    IBKR -->|"1. SendRequest\n2. Wait 10s\n3. GetStatement"| IW
    TR <-->|"Timeline & Portfolio"| TW
    TW <-->|"Ready-First 2FA\n(Gateway MESSAGE_CREATE)"| DISCORD
    TW <--> TRSESS
    IW <--> SQLITE
    TW <--> SQLITE
    IW --> BB_REC
    IW --> BB_ACC
    TW --> BB_REC
    TW --> BB_ACC
```

## 2. Module Boundaries

### `config/settings.py`
- Centralizes all environment configuration using `pydantic_settings.BaseSettings`.
- Holds normalized Slovenian/European merchant keyword rules (`DEFAULT_MERCHANT_CATEGORY_RULES`), ISO 18245 `MCC_CATEGORY_MAP`, and Trade Republic semantic hint rules (`TR_HINT_CATEGORY_MAP`) for card transactions (see ADR-0014).
- Exposes `use_mock_data`, `dry_run`, `sync_start_time` (`SYNC_START_TIME`), and `sync_timezone` (`SYNC_TIMEZONE`) settings.

### `shared/scheduler.py`
- Implements `parse_start_time`, `resolve_timezone`, `compute_next_scheduled_run`, and `run_periodic_sync` shared by `ibkr_worker` and `tr_worker` (see ADR-0009).
- Runs immediately on startup when `SYNC_START_TIME` is unset or when today's start time has already passed (anchoring subsequent runs to tomorrow's `SYNC_START_TIME + k * interval`), or waits until today's `SYNC_START_TIME` when started earlier.

### `shared/discord.py`
- Shared Discord REST API v10 client (including `get_gateway_url()` for Gateway WebSocket discovery) and optional `DiscordErrorHandler` (`ERROR`-level `logging.Handler`) attached by both `ibkr_worker` and `tr_worker` when `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID` are configured (`has_discord == True`), with secret masking, 2000-character truncation, and dry-run/mock suppression (see ADR-0010).

### `shared/database.py`
- Encapsulates SQLite connection management with `PRAGMA journal_mode=WAL;` and `PRAGMA busy_timeout=5000;` so `ibkr_worker` and `tr_worker` can safely read/write the shared `/app/data/finance_sync.db` concurrently.
- Provides `compute_record_hash(broker, external_id, timestamp, amount)` using `hashlib.sha256`.
- Manages `seen_records` and `sync_metadata` tables.

### `shared/budgetbakers.py`
- Wraps `httpx.Client` with Bearer JWT authentication and automatic `429 Retry-After` backoff.
- Enforces BudgetBakers API batch constraints:
  - `POST /v1/api/records`: chunks into slices of at most `20` items.
  - `DELETE /v1/api/records`: chunks into slices of at most `10` IDs.
  - `PATCH /v1/api/accounts`: computes `initialBalance = round(target_balance - totalIncomes + totalExpenses, 2)` and sends `[{"id": account_id, "initialBalance": initialBalance}]`.
- Supports read-only **Dry-Run Mode** (`dry_run=True`): blocks all `POST`, `DELETE`, and `PATCH` mutations while recording preview payloads (`dry_run_created`, `dry_run_deleted_ids`, `dry_run_patched_accounts`) and performing read-only `GET` queries (with offline stub fallback when `use_mock_data=True`).

### `ibkr_worker/`
- `flex_client.py`: Executes the two-step IBKR Flex Web Service protocol (`FlexClient`), parses XML bytes via `ibflex2` (`parse_flex_xml`) into typed `IBKRStatementData`, `IBKRTradeEvent`, `IBKROpenPosition`, and `IBKRCashEvent` models (converting `OpenPositions` to base currency via `fxRateToBase` and skipping internal `assetCategory="CASH"` FX conversion rows), and merges multi-year historical XML statements (`merge_statement_data`).
- `worker.py`: Orchestrates polling (`IBKRWorker`), optional full account record purge (`--purge-account-records`) or targeted deletion from a local gitignored JSON file (`--delete-file`), local historical XML file/directory ingestion (`--xml-file`), CouchDB stock holdings sync (`CouchDBStockSyncClient` with `zero_cost_baseline_assets=settings.zero_cost_baseline_asset_set` per ADR-0012), automatic existing-record seeding on fresh SQLite volumes, record creation on `BB_IBKR_ACCOUNT_ID`, live uninvested cash balance patching, and CLI flags (`--once`, `--dry-run`, `--dry-run-output`, `--purge-account-records`, `--delete-file`, `--xml-file`).

### `tr_worker/`
- `session_manager.py`: Wraps `pytr` session persistence in `/app/data` (`TRSessionManager`), fetches `timeline_detail_v2` for trade/dividend/Saveback and post-cutoff card transactions (see ADR-0014), and implements `Discord2FAClient` using Discord Gateway v10 (`MESSAGE_CREATE` event subscription with zero timeout and ready-first login initiation) to receive a 4-to-6-digit verification code and retry indefinitely on login failure (see ADR-0011).
- `event_parser.py`: Pure functional parser (`parse_tr_timeline_events`, `parse_tr_portfolio_value`, `parse_tr_stock_trades`, `parse_tr_live_positions`) converting raw Trade Republic timeline & portfolio JSON payloads into typed `ParsedTRRecord` (`TRRecordKind`) and `StockTradeInput` objects with target account routing (`cash` vs. `wealth`), paired `Transfer` creation, zero-cost (`cashAmount=0`, `price=0`) `1% Saveback` holdings (see ADR-0013), detail/MCC/icon category resolution (see ADR-0014), and stock buy `note` formatting.
- `worker.py`: Orchestrates initial existing-record seeding (`TRWorker`), optional full account purge (`--purge-account-records`) or targeted deletion from a local gitignored JSON file (`--delete-file`), syncs new timeline events and CouchDB stock holdings per batch (skipping SQLite/CouchDB mutations in dry-run mode), patches `BB_TR_WEALTH_ACCOUNT_ID` balance, and supports `--once`, `--dry-run`, `--dry-run-output`, `--purge-account-records`, and `--delete-file`.
