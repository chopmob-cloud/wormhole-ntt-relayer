# base_to_algo/ — Base → Algorand relay pipeline

The Base→Algorand direction requires **two separate on-chain steps** because Algorand's opcode budget cannot accommodate both Wormhole guardian signature verification and token minting in a single atomic transaction group. The pipeline is split accordingly:

```
relay_service.py  (orchestrator — polls, tracks state, calls Step 1 then Step 2)
      │
      ├── Step 1: base-relayer.py  →  Wormhole guardian verification + attestation
      │
      └── Step 2: ntt_execute.py  →  NTT_MGR.execute_message() → mints tokens
```

Runs as `base-algo-relayer.service` (systemd), working directory `/opt/relayers/base_to_algo/`.

---

## relay_service.py

### Purpose

Production orchestrator. Polls WormholeScan every 30 seconds, manages per-sequence state in SQLite, and drives each sequence through the two-step relay pipeline. Handles retries, graceful shutdown, and resume after restart.

### Layout

```
Config
  RELAY_DIR      directory of this script (used for relative imports)
  ENV_FILE       path to .env (env var override)
  DB_FILE        path to .relay.db (env var override)
  POLL_INTERVAL  30 seconds (env var override)
  EMITTER        Base WormholeTransceiver emitter address (hex, chain 30)

Logging
  structured ISO timestamp logging to stdout (captured by journald)

Graceful shutdown
  _running flag + SIGTERM/SIGINT handlers — poll loop checks flag
  each second of the sleep interval so shutdown is near-immediate

Database (get_db, db_get, db_upsert, db_last_complete, db_increment_attempts)
  SQLite with Row factory. Each sequence has:
    status: pending | attested | complete | failed
    attempt counter + last_error for diagnostics
  db_upsert handles insert-or-update with datetime('now') for timestamps

WormholeScan (fetch_new_vaas, fetch_vaa_b64)
  fetch_new_vaas(after_seq) — lists all VAAs beyond last complete, sorted ascending
  fetch_vaa_b64(seq)        — fetches the base64 VAA bytes for a specific sequence

Step delegation (step1_attest, step2_execute)
  Both use importlib.util to dynamically load base_relayer.py and ntt_execute.py
  as modules at runtime. This avoids circular imports and lets relay_service.py
  live in the same directory as its dependencies without a package structure.
  "already in ledger" errors on step1 are treated as success (idempotent resume).

process_sequence(seq)
  Full state machine for one sequence:
    - Skip if complete
    - Mark failed after 5 attempts
    - Fetch VAA
    - Step 1 (skip if already attested)
    - 3-second sleep to let on-chain state settle
    - Step 2
    - Mark complete

main()
  Initialises DB, finds last complete sequence, then loops:
    - Queries for any pending/attested sequences to retry
    - Fetches new VAAs beyond last complete
    - Processes all in ascending sequence order
    - Breaks out of inner loop on hard failure (doesn't skip ahead)
```

### How it was developed

The earlier `base_to_algo_relayer.py` was a simpler script that submitted a 2-txn group without full guardian verification. It was replaced by the full pipeline (`base_relayer.py` + `ntt_execute.py`) when it became clear the simpler approach was failing — the Algorand contract requires the complete Wormhole verifySigs + verifyVAA flow.

`relay_service.py` was written on **Mar 2, 2026** as the final production wrapper. It was not in the `~/wormhole-relayer` dev directory — it was written directly in `/opt/relayers/base_to_algo/` as the production-only orchestration layer. The SQLite schema in this file is richer than the earlier relayers (full status tracking, timestamps, per-step tx IDs) reflecting lessons learned from the first few days of operation.

---

## base-relayer.py

### Purpose

Step 1 of the Base→Algorand relay. Submits the full Wormhole guardian verification and attestation transaction group to the Algorand WormholeTransceiver. After this step, `TransceiverManager` has recorded the attestation in the `num_attestations_<digest>` box.

> **Note on filename:** The file uses a hyphen (`base-relayer.py`) to match the `importlib.util.spec_from_file_location` call in `relay_service.py` (line 138). The module is loaded dynamically by path, so the hyphen in the filename has no import implications.

Can be run standalone: `python3 base-relayer.py <sequence> [--dry-run]`

### Layout

```
Config / env loading
  _load_env()  custom .env parser (not python-dotenv, avoids dependency)
  _cfg() / _cfg_int()  env-with-fallback helpers
  WORMHOLE_CORE, WT_APP, NTT_MGR, TRANSCEIVER_MGR, NTT_TOKEN_ASSET  — all overridable
  TMPL_BYTECODE  base64-encoded Wormhole template LogicSig bytecode

Low-level helpers
  keccak256(data)           pycryptodome Keccak-256
  encode_uvarint(val)       AVM uvarint encoding (for tmplsig patching)
  app_addr(app_id)          algosdk application address
  fresh_sp(client)          suggested_params with flat_fee + fresh round window

Template LogicSig
  populate_tmplsig(addr_idx, emitter_id, app_id, app_addr_b)
    Patches the 4 template variables (TMPL_ADDR_IDX, TMPL_EMITTER_ID,
    TMPL_APP_ID, TMPL_APP_ADDRESS) into the Wormhole vaa_verify bytecode.
    Uses uvarint encoding for integers and length-prefixed bytes.
  get_tmplsig(addr_idx, emitter_id)
    Returns (address, LogicSigAccount) for a given template instantiation.

VAA parsing
  parse_vaa(vaa_bytes)
    Extracts: guardian_set_index, num_sigs, signatures, body, emitter_chain,
    emitter_address, sequence, payload, vaa_digest (keccak256(keccak256(body))),
    ntt_prefix.

Guardian state
  get_guardian_keys(client, guardian_addr)
    Reads guardian public keys from Wormhole Core local state on the guardian
    template address. Returns concatenated 20-byte key chunks in key order.
  load_vaa_verify(client)
    Compiles vaa_verify.teal (from /tmp/wormhole/...) via Algod compile endpoint,
    caches to /tmp/vaa_verify_program.b64. Verifies hash matches vphash in
    Wormhole Core global state.
  ensure_optin(client, sender_addr, sender_key, app_id, addr_idx, emitter_id)
    Opts the sequence template address into Wormhole Core if not already opted in.
    Funds it if balance < 1,002,000 µA.

NTT payload parsing
  parse_ntt(parsed)
    Extracts ntt_digest and recipient Algorand address from the VAA payload.
    Checks for NTT prefix (9945ff10).
    Recipient is at inner_body[45:77].
    ntt_digest = keccak256(msg_id + user_univ + chain + src_ntt + handler + payload)
  is_consumed(digest)
    Checks vaas_consumed_<digest> box on WormholeTransceiver — early exit if
    already relayed.

Transaction building
  build_pre_txns(...)
    Builds:
      [optional] PaymentTxn to fund vaa_verify LogicSig (if balance < 100k µA)
      N × verifySigs ApplicationCallTxn (9 sigs per txn, fee=0, sent from vaa_verify)
      1 × verifyVAA ApplicationCallTxn (fee = 1000 × (1 + blocks))
        - Declares NTT_MGR as foreign_app (AVM v7 group-wide resource sharing)
        - Declares ntt_manager_peer_ box on NTT_MGR
        - Adds recipient account and NTT_MGR app address
  build_atc(...)
    Wraps pre_txns + verifyVAA into an ATC with:
      receive_message(appl)void call on WT_APP
      Box refs: wormhole_peer_, vaas_consumed_, handler_transceivers_,
                handler_paused_, num_attestations_, attestations_
      random 8-byte note on receive_message to prevent deterministic txid
      collision on retry

Main entry
  relay_vaa(vaa_b64, sender_mnemonic, dry_run=False)
    Full sequence: parse → check consumed → load verify program → ensure optin
    → get guardian keys → parse NTT → build txns → submit ATC
  __main__ block
    CLI: sequence [--emitter] [--chain] [--dry-run]
    Fetches VAA from WormholeScan, calls relay_vaa()
```

### Box references used

All declared on `receive_message` call (app_index 0 = WT_APP, app_index 1 = TRANSCEIVER_MGR):

| Box | App | Purpose |
|---|---|---|
| `wormhole_peer_<chain_2b>` | WT_APP | Validates Base is a registered peer |
| `vaas_consumed_<vaa_digest>` | WT_APP | Written by contract to mark VAA consumed |
| `handler_transceivers_<ntt_mgr_8b>` | TRANSCEIVER_MGR | Verifies WT_APP is a registered transceiver |
| `handler_paused_<ntt_mgr_8b>` | TRANSCEIVER_MGR | Checks if relaying is paused |
| `num_attestations_<ntt_digest>` | TRANSCEIVER_MGR | Written by contract: threshold tracking |
| `attestations_<ntt_digest><wt_app_8b>` | TRANSCEIVER_MGR | Records this transceiver's attestation |

`ntt_manager_peer_<chain_2b>` on NTT_MGR is declared on the `verifyVAA` txn (AVM v7 group-wide sharing).

### How it was developed

This is the most complex file in the repo. It was written on **Mar 2, 2026** after `base_to_algo_relayer.py` failed — the Algorand Wormhole contract requires the full guardian signature verification pipeline, which the simpler version skipped.

The implementation was developed by:
1. Reading the Wormhole Algorand SDK source (Python) for the template LogicSig logic and `verifySigs` batching
2. Running `inspect_tx.py` on successful on-chain transfers to extract the exact box references and foreign apps needed
3. Iteratively adding box references as the contract returned `invalid Box reference 0x...` errors — each error revealed the next missing box by its hex name, which was decoded and added
4. The AVM v7 group-wide resource sharing trick (putting NTT_MGR on verifyVAA instead of receive_message) was needed to stay within the per-transaction box reference limit of 8

The `random.randint` on the payment amount and `os.urandom(8)` note on receive_message were added after discovering that deterministic transaction IDs caused "already in ledger" errors on retry.

---

## ntt_execute.py

### Purpose

Step 2 of the Base→Algorand relay. Calls `NTT_MGR.execute_message(MessageReceived)` after attestation has been recorded by Step 1. This is the call that actually mints or unlocks the token ASA to the recipient address on Algorand.

Can be run standalone: `python3 ntt_execute.py <sequence> [--dry-run] [--debug]`

### Layout

```
Config / env loading
  Same _load_env / _cfg / _int pattern as base_relayer.py
  Shared constants: ALGOD_URL, WT_APP, NTT_MGR, TRANSCEIVER_MGR, NTT_TOKEN_ASSET

Helpers
  keccak256(d)       pycryptodome Keccak-256
  fresh_sp(client, fee=6000)  suggested params, flat fee
  fetch_box(app_id, name)     GET /v2/applications/{app_id}/box?name=b64:...
                               returns decoded bytes or None

VAA parsing
  parse_vaa(vaa_bytes)
    Lighter version than base_relayer.py — only extracts what execute_message needs:
    emitter_chain, sequence, payload, vaa_digest, body, raw.

NTT payload parsing
  parse_ntt_fields(parsed)
    Full documented layout of outer payload + mp + inner_body:
      src_ntt    p[4:36]   source NTT manager (32b)
      dst_ntt    p[36:68]  destination NTT manager (32b)
      mplen      p[68:70]
      mp         p[70..]
        msg_id       mp[0:32]
        user_univ    mp[32:64]
        inner        mp[64:]  (length prefix + NativeTokenTransfer body)
          from_decimals  inner_body[4]
          from_amount    inner_body[5:13]
          source_token   inner_body[13:45]
          recipient      inner_body[45:77]  → Algorand address
          recipient_chain inner_body[77:79]
    Computes two digests:
      ntt_digest   used for TRANSCEIVER_MGR boxes (same formula as base_relayer.py)
      msg_digest   used for NTT_MGR messages_executed box
    Returns all parsed fields as a dict.

Pre-flight checks (preflight)
  Five checks before submitting:
    1. dst_ntt matches this NTT_MGR (zero-padded app ID)
    2. recipient_chain == 8 (Algorand)
    3. src_ntt matches registered peer in ntt_manager_peer_ box
    4. num_attestations_ box exists (Step 1 was completed)
    5. messages_executed_ box does NOT exist (not already done)
  --debug flag prints all raw field values

ATC construction (build_execute_atc)
  Single ATC method call on NTT_MGR:
    method: execute_message((byte[32],byte[32],uint16,byte[32],byte[32],byte[]))void
    args:   [msg_id, user_univ, emitter_chain, src_ntt, dst_ntt, inner]
    boxes (app_index 0 = NTT_MGR, app_index 1 = TRANSCEIVER_MGR):
      messages_executed_<msg_digest>         written by contract on success
      ntt_manager_peer_<chain_2b>            peer validation
      num_attestations_<ntt_digest>          read by contract to check threshold
      attestations_<ntt_digest><wt_app_8b>   read by contract to verify attesters
      handler_transceivers_<ntt_mgr_8b>      transceiver whitelist check
      handler_paused_<ntt_mgr_8b>            pause guard
    accounts: [recipient_addr]

Main entry
  execute_ntt(vaa_b64, sender_mnemonic, dry_run, debug)
    parse VAA → parse NTT fields → preflight → build ATC → execute
  __main__ block
    CLI: sequence [--emitter] [--chain] [--dry-run] [--debug]
```

### How it was developed

Written alongside `base_relayer.py` on **Mar 2, 2026** after the two-step nature of the relay was understood. The `MessageReceived` struct layout was reverse-engineered by:

1. Reading the Folks Finance PuyaPy NTT contract source (referenced in `ntt_digest.py`)
2. Tracing the `execute_message` method signature and its argument types
3. Running `--debug` mode on test sequences and comparing field values against on-chain indexer data to verify the byte offsets

The `msg_digest` formula (distinct from `ntt_digest`) was determined by examining the `messages_executed_` box key format in the NTT_MGR contract. The two digests use overlapping but different input sets — `ntt_digest` uses `handler_address` as the zero-padded app ID, while `msg_digest` uses `dst_ntt` (the universal address from the VAA) — a subtle difference that caused errors until verified.

The pre-flight check system was added after several wasted transactions hit contract asserts. Running checks against on-chain boxes first catches common failure modes (Step 1 not done, wrong chain, already executed) without spending gas.

---

## requirements.txt

Python dependencies for `base-relayer.py`, `ntt_execute.py`, and `relay_service.py`:

```
py-algorand-sdk>=2.5.0
pycryptodome>=3.19.0
requests>=2.31.0
python-dotenv>=1.0.0
```

Install via: `pip install -r requirements.txt` (or use `setup.sh` which handles venv creation).

---

## setup.sh

### Purpose

Server setup script. Creates a Python venv, installs dependencies, writes a `.env` template, downloads `vaa_verify.teal`, and installs the systemd service — all in one pass.

```bash
bash setup.sh
```

### Layout

```
Step 1 — System deps
  apt-get install python3 python3-pip python3-venv curl

Step 2 — Python venv
  python3 -m venv venv/
  pip install -r requirements.txt

Step 3 — .env file
  Creates .env if absent with ALGO_MNEMONIC placeholder and commented app ID defaults.
  chmod 600 .env

Step 4 — vaa_verify TEAL program
  Required by base-relayer.py at /tmp/wormhole/algorand/teal/vaa_verify.teal
  Downloads from Wormhole GitHub repo if not already present.
  base-relayer.py compiles it via Algod on first run and caches to /tmp/vaa_verify_program.b64.

Step 5 — Systemd service
  Writes /etc/systemd/system/base-algo-relayer.service
  systemctl daemon-reload && systemctl enable base-algo-relayer

Summary
  Prints next steps: set ALGO_MNEMONIC, run dry-run test, start service.
```

### Critical dependency: vaa_verify.teal

`base-relayer.py` requires `vaa_verify.teal` to compile the Wormhole template LogicSig. This TEAL file is not included in the repo (it is part of the Wormhole SDK). `setup.sh` downloads it from the Wormhole GitHub repo during setup. If the download fails, the file can be copied from an existing relay server at `/tmp/vaa_verify_program.b64`.

### How it was developed

Adapted from the `/opt/relayers/base_to_algo/setup.sh` script found on the production server. The original had a `STATE_FILE` environment variable that `relay_service.py` never reads — this was removed. The `vaa_verify.teal` download step was added because the production server had this file in place but the repo did not, making fresh deploys fail silently.
