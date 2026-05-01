# db/ — SQLite databases

Local copies of the production SQLite databases pulled from the server. These are snapshots — the live databases are in `/opt/relayers/algo_to_base/.relayer.db` and `/opt/relayers/base_to_algo/.relay.db` on the Ubuntu instance.

The databases are excluded from git (`.gitignore` covers `*.db`) but kept in the `db/` folder for local inspection and analysis.

---

## algo_to_base.relayer.db

Used by: `algo_to_base/cast_relayer.py`

### Schema

```sql
CREATE TABLE relayed (
    sequence  TEXT PRIMARY KEY,   -- Wormhole sequence number (stored as text)
    txHash    TEXT,               -- Base transaction hash (0x-prefixed)
    relayedAt INTEGER             -- Unix timestamp of relay
);
```

### Purpose

Prevents double-submission. Before attempting a relay, `cast_relayer.py` checks `has_relayed(seq)`. After a successful `cast send`, `mark_relayed(seq, tx_hash)` is called.

There is no retry tracking. If a sequence fails it stays absent from this table and will be retried on the next poll. If it needs to be permanently skipped (e.g. not an NTT VAA), it must be inserted manually with a sentinel `txHash`.

### Notes

- `sequence` is stored as TEXT not INTEGER — this means `ORDER BY sequence` returns lexicographic order (1, 10, 11, 2...) rather than numeric. This has not caused operational issues because the relayer processes all unrelayed sequences each poll regardless of order.
- No `status` column — the only states are "in table" (done) and "not in table" (pending/retry).

---

## base_to_algo.relay.db

Used by: `base_to_algo/relay_service.py`

### Schema

```sql
CREATE TABLE sequences (
    sequence     INTEGER PRIMARY KEY,
    status       TEXT NOT NULL DEFAULT 'pending',
    vaa_digest   TEXT,    -- hex, from parsed VAA body keccak256(keccak256(body))
    ntt_digest   TEXT,    -- hex, computed NTT message digest
    recipient    TEXT,    -- Algorand address of token recipient
    amount       INTEGER, -- raw token amount from inner_body
    attest_txid  TEXT,    -- Algorand tx ID of Step 1 (base_relayer.py)
    execute_txid TEXT,    -- Algorand tx ID of Step 2 (ntt_execute.py)
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,    -- last exception message, for diagnostics
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
```

### Status lifecycle

```
pending   →  attested  →  complete
    ↓                         ↑
    └──────── failed ──────────┘  (after 5 attempts)
```

- `pending` — VAA seen, Step 1 not yet submitted
- `attested` — Step 1 (base_relayer.py) confirmed on-chain; waiting for Step 2
- `complete` — Step 2 (ntt_execute.py) confirmed; tokens minted
- `failed` — max attempts (5) reached without success

### Notes

- The `vaa_digest`, `ntt_digest`, `recipient`, `amount`, `attest_txid`, `execute_txid` columns exist in the schema but were **not populated** in the production records — `relay_service.py` only writes `status`, `attempts`, and `last_error` via `db_upsert`. These columns were designed for richer diagnostics but the implementation only uses the minimum needed to drive the state machine.
- `db_upsert` uses a sentinel string `"datetime('now')"` to inject SQL functions into the UPDATE/INSERT. This is an unusual pattern — it works but means the string `"datetime('now')"` cannot be stored as a literal `last_error` value.
- The `sequence` column is INTEGER (unlike `algo_to_base.relayer.db` where it is TEXT), so ordering is numeric.

---

## Querying locally

Since `sqlite3` CLI is not available on Windows, use Python:

```python
import sqlite3

# Algo → Base summary
conn = sqlite3.connect('db/algo_to_base.relayer.db')
for row in conn.execute('SELECT * FROM relayed ORDER BY CAST(sequence AS INTEGER)'):
    print(row)

# Base → Algo by status
conn = sqlite3.connect('db/base_to_algo.relay.db')
for row in conn.execute("SELECT sequence, status, attempts, last_error FROM sequences ORDER BY sequence"):
    print(row)
```
