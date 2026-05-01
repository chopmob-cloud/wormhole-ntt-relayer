# algo_to_base/cast_relayer.py

## Purpose

Production service for the **Algorand → Base** relay direction. Watches WormholeScan for signed VAAs emitted by the Algorand NTT WormholeTransceiver and submits them to the Base WormholeTransceiver to unlock/mint tokens on Base.

Runs continuously as `algo-base-relayer.service` (systemd), working directory `/opt/relayers/algo_to_base/`.

---

## Layout

```
Constants
  CAST_BIN       path to Foundry cast binary
  RPCURL         Base RPC endpoint (env: RPCURL)
  PRIVATEKEY     Base account private key, 64 hex chars no 0x (env: PRIVATEKEY)
  CHAIN_ID       "8" — Algorand's Wormhole chain ID
  EMITTER_ADDRESS  Algorand NTT WormholeTransceiver emitter (env: ALGO_EMITTER_ADDRESS)
  TRANSCEIVER    Base WormholeTransceiver contract address (env: BASE_TRANSCEIVER)
  POLL_INTERVAL  20 seconds
  DB_PATH        ".relayer.db"

Database helpers
  init_db()           creates relayed table, returns connection
  has_relayed(conn, sequence)   checks if sequence is already in DB
  mark_relayed(conn, sequence, tx_hash)  records a completed relay

WormholeScan
  fetch_recent_vaas()   GET /api/v1/vaas/{chain}/{emitter}
                        returns list of VAA objects from WormholeScan

Cast
  cast_redeem(vaa_hex)  runs `cast send TRANSCEIVER receiveMessage(bytes) VAA_HEX`
                        via subprocess, parses JSON output for transactionHash

Relay loop
  relay_once(conn)      iterates VAAs, skips known sequences and non-v1 VAAs,
                        calls cast_redeem, records result
  main()                validates env, opens DB, loops relay_once every 20s
```

---

## How it works

1. `fetch_recent_vaas()` calls WormholeScan's `/vaas` endpoint for chain 8 (Algorand), emitter address of the Algorand WormholeTransceiver.
2. For each VAA not already in the DB, the base64 VAA is decoded to a `0x`-prefixed hex string.
3. The first byte (VM version) is checked — only version `0x01` is supported.
4. `cast_redeem()` calls Foundry's `cast send` with `--json` flag, captures stdout, and parses `transactionHash`.
5. On success the sequence is written to SQLite. On failure the error is logged and the sequence is retried next poll.

---

## Database

File: `.relayer.db` (in working directory)

```sql
CREATE TABLE relayed (
    sequence  TEXT PRIMARY KEY,
    txHash    TEXT,
    relayedAt INTEGER   -- unix timestamp
);
```

No retry tracking. If a sequence fails it will be retried every poll cycle until it succeeds or is manually skipped.

---

## How it was developed

**Origin: Feb 23, 2026** — the earliest file in the repo (`relay-once.sh`) was a shell script that hardcoded one sequence and called `cast send` manually. That proved the Base contract call worked.

**Feb 28** — `relay.py` was written as the first polling loop, using Web3.py instead of `cast`. It used the richer WormholeScan `/operations` endpoint and filtered by `app_id == NATIVE_TOKEN_TRANSFER` and recipient NTT manager.

**Mar 2** — `cast_relayer.py` was written as a simpler alternative to `relay.py`. The reason for switching from Web3.py to `cast` was likely to reduce Python dependency surface and leverage Foundry's already-present CLI on the server. The `/vaas` endpoint (simpler than `/operations`) was used since the only filtering needed is VM version — all VAAs from this emitter are NTT transfers.

`cast_relayer.py` became the production service. `relay.py` was retained as a backup but never ran in production.

---

## Key differences vs relay.py (removed)

| | cast_relayer.py | relay.py |
|---|---|---|
| Submission method | `cast send` subprocess | Web3.py direct |
| WormholeScan endpoint | `/vaas` | `/operations` |
| Filtering | VM version only | app_id + recipient NTT manager |
| Status | **Production** | Removed (unused) |
