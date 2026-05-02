# ntt-relayer

A custom off-chain relayer for [Wormhole NTT (Native Token Transfer)](https://wormhole.com/docs/learn/messaging/native-token-transfers/overview/) bridging between **Algorand** and **Base**. Runs as two systemd services on a Linux server.

The relayer watches WormholeScan for signed VAAs and submits them to the destination chain's NTT contracts. No funds are held by the relayer — it only pays gas on behalf of the bridge.

The Algorand NTT contracts are implemented by [Folks Finance](https://folks.finance) in PuyaPy. The digest formula in `utils/ntt_digest.py` is derived from their on-chain `calculate_message_digest` implementation.

---

## Architecture

```
Algorand ──► WormholeScan ──► algo_to_base/cast_relayer.py
                                   └── cast send receiveMessage(bytes)  ──► Base (1 step)

Base ──► WormholeScan ──► base_to_algo/relay_service.py
                               ├── Step 1: base-relayer.py
                               │         └── receive_message(appl)void  ──► Algorand WormholeTransceiver
                               └── Step 2: ntt_execute.py
                                         └── execute_message(...)void   ──► Algorand NTT Manager
                                                                              └── mints/unlocks token ASA
```

### Why Base→Algorand requires two steps

Algorand has a per-transaction opcode budget cap. Verifying 19 Wormhole guardian signatures requires batching across multiple `verifySigs` application calls, each consuming a large portion of the budget. There is not enough remaining budget in a single atomic group to also call `execute_message` and mint tokens. The protocol solves this by splitting the relay into two separate atomic groups:

1. **Step 1 — Attest:** Submit the full guardian signature verification. The `WormholeTransceiver` records an attestation in `TransceiverManager`.
2. **Step 2 — Execute:** Once the attestation threshold is met, call `NTT Manager.execute_message()`. The manager checks the attestation exists, applies rate-limiting, and mints or unlocks the token ASA to the recipient.

The Algorand→Base direction has no equivalent constraint — a single `cast send` call to the EVM `WormholeTransceiver.receiveMessage(bytes)` handles everything.

---

## Relay flows in detail

### Algorand → Base

1. `cast_relayer.py` polls WormholeScan every 20 s for new VAAs from the Algorand NTT emitter.
2. For each unseen sequence, it decodes the base64 VAA to `0x`-prefixed hex and checks the VM version byte (only v1 supported).
3. Calls `cast send <BASE_TRANSCEIVER> "receiveMessage(bytes)" <VAA_HEX>` via Foundry CLI subprocess.
4. On success the sequence and Base transaction hash are written to SQLite (`.relayer.db`).
5. Failed sequences stay absent from the DB and are retried on the next poll.

### Base → Algorand

`relay_service.py` is the orchestrator. It polls WormholeScan every 30 s, maintains a richer SQLite state machine (`pending → attested → complete | failed`), and drives each sequence through both steps:

**Step 1 — `base-relayer.py`**

The Wormhole Algorand SDK uses a LogicSig program (`vaa_verify.teal`) that must be instantiated ("populated") for each unique `(guardian_set_index, emitter_id)` pair. The relayer:

- Compiles `vaa_verify.teal` via the Algod compile endpoint and caches the result.
- Patches four template variables (`TMPL_ADDR_IDX`, `TMPL_EMITTER_ID`, `TMPL_APP_ID`, `TMPL_APP_ADDRESS`) into the bytecode at known offsets using AVM uvarint encoding.
- Ensures the resulting LogicSig address is opted-in to Wormhole Core and funded.
- Fetches the 19 guardian public keys from Wormhole Core app local state.
- Builds an atomic transaction group:
  - Optional `PaymentTxn` to fund the LogicSig if balance is low.
  - N × `verifySigs` application calls (9 signatures per call, sent from the LogicSig).
  - 1 × `verifyVAA` application call (declares NTT Manager as a foreign app for AVM v7 group-wide resource sharing).
  - 1 × `receive_message(appl)void` ATC method call on `WormholeTransceiver`.

**Step 2 — `ntt_execute.py`**

- Parses the NTT payload from the VAA to extract `msg_id`, `user_univ`, `src_ntt`, `dst_ntt`, `inner`, and the recipient Algorand address.
- Computes two separate digests: `ntt_digest` (key for `TransceiverManager` attestation boxes) and `msg_digest` (key for `NTT Manager` executed messages box).
- Runs five pre-flight checks against on-chain box state before submitting (wrong chain, peer mismatch, attestation missing, already executed).
- Submits a single ATC method call to `NTT Manager.execute_message(MessageReceived)`.

---

## Repo layout

```
algo_to_base/
  cast_relayer.py          Production service — Algorand→Base relay loop
  requirements.txt

base_to_algo/
  relay_service.py         Production service — orchestrator, SQLite state machine
  base-relayer.py          Step 1 — guardian sig verification + Wormhole attestation
  ntt_execute.py           Step 2 — NTT execute_message → mints token ASA
  setup.sh                 Server setup: venv, deps, .env template, vaa_verify.teal, systemd
  requirements.txt

utils/
  ntt_digest.py            Pure-Python Keccak-256 + NTT message digest formula (reference)
  ntt_debug.py             Full VAA diagnostic — parses payload, checks all on-chain boxes
  check_consumed.py        Quick check: has a given sequence been consumed on Algorand?
  inspect_tx.py            Decode Algorand transaction(s) — foreign apps, args, inner txns
  relay-once.sh            Manually relay a single Algorand→Base sequence via cast send
  update_ntt_addresses.sh  Migrate source files when Base contracts are redeployed
  update_algorand_peers.py Update Algorand-side peer registry after a Base redeploy

deploy/
  algo-base-relayer.service    systemd unit for Algorand→Base service
  base-algo-relayer.service    systemd unit for Base→Algorand service

db/                        Local SQLite snapshots — excluded from git, not production
docs/                      Per-file deep-dive documentation
```

---

## Prerequisites

| Requirement | Used by |
|---|---|
| Python 3.10+ | All Python services |
| [Foundry](https://getfoundry.sh) (`cast`) | `cast_relayer.py`, `relay-once.sh` |
| `jq` | `relay-once.sh` |
| `node` (any recent LTS) | `relay-once.sh` (base64→hex conversion) |
| Linux with systemd | Production deployment |

The Base→Algorand service additionally requires `vaa_verify.teal` to be present on the server. `setup.sh` downloads it from the Wormhole GitHub repo automatically. Without it, `base-relayer.py` cannot compile the Wormhole template LogicSig and will fail on first use.

---

## Configuration

All configuration is via environment variables, typically loaded from a `.env` file.

```bash
cp .env.example .env
# Edit .env — every blank value must be filled in before starting the services
```

### Algorand → Base (`algo_to_base/.env` or shared)

| Variable | Required | Description |
|---|---|---|
| `PRIVATEKEY` | Yes | Base wallet private key — 64 hex chars, **no** `0x` prefix |
| `BASE_TRANSCEIVER` | Yes | Base `WormholeTransceiver` contract address (`0x`-prefixed) |
| `ALGO_EMITTER_ADDRESS` | Yes | Algorand NTT transceiver emitter address — 32-byte hex, no `0x` |
| `RPCURL` | No | Base RPC endpoint (default: `https://mainnet.base.org`) |
| `CAST_BIN` | No | Path to `cast` binary (default: `/home/ubuntu/.foundry/bin/cast`) |

### Base → Algorand (`base_to_algo/.env` or shared)

| Variable | Required | Description |
|---|---|---|
| `ALGO_MNEMONIC` | Yes | 25-word Algorand mnemonic for the relayer wallet |
| `BASE_EMITTER` | Yes | Base `WormholeTransceiver` emitter — 32-byte hex, no `0x` |
| `WT_APP_ID` | Yes | Algorand `WormholeTransceiver` app ID |
| `NTT_MGR_APP_ID` | Yes | Algorand `NTT Manager` app ID |
| `TRANSCEIVER_MGR_APP_ID` | Yes | Algorand `TransceiverManager` app ID |
| `NTT_TOKEN_ASSET_ID` | Yes | Token ASA ID on Algorand |
| `ALGOD_URL` | No | Algod endpoint (default: `https://mainnet-api.4160.nodely.dev`) |
| `ALGOD_TOKEN` | No | Algod API token (default: empty) |
| `BASE_CHAIN` | No | Source Wormhole chain ID (default: `30` = Base) |
| `POLL_INTERVAL` | No | Seconds between polls (default: `30`) |
| `WORMHOLE_CORE_APP_ID` | No | Override Wormhole Core app (default: `842125965`) |

---

## Setup

### Base → Algorand service

```bash
cd base_to_algo
bash setup.sh
# setup.sh:
#   - installs python3, pip, venv
#   - creates venv/ and installs requirements.txt
#   - writes .env template (edit it next)
#   - downloads vaa_verify.teal from Wormhole GitHub
#   - installs and enables base-algo-relayer.service via systemd
```

After setup, fill in `.env`, then test a single sequence before starting:

```bash
source venv/bin/activate

# Dry-run Step 1 (no transaction submitted)
python base-relayer.py <sequence> --dry-run

# Dry-run Step 2
python ntt_execute.py <sequence> --dry-run

# Start the service
sudo systemctl start base-algo-relayer
sudo journalctl -u base-algo-relayer -f
```

### Algorand → Base service

```bash
cd algo_to_base
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

Copy `deploy/algo-base-relayer.service` to `/etc/systemd/system/`, edit paths, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now algo-base-relayer
sudo journalctl -u algo-base-relayer -f
```

---

## Running

### Service management

```bash
# Status
sudo systemctl status base-algo-relayer
sudo systemctl status algo-base-relayer

# Logs (live)
sudo journalctl -u base-algo-relayer -f
sudo journalctl -u algo-base-relayer -f

# Restart
sudo systemctl restart base-algo-relayer
```

### Manual one-shot relay (Algorand→Base)

Useful for backfilling a missed sequence without the full service running:

```bash
# Requires BASE_TRANSCEIVER, PRIVATE_KEY, ALGO_EMITTER in .env
utils/relay-once.sh <sequence>
utils/relay-once.sh <sequence> --env /path/to/.env
```

---

## Diagnostics

### Check whether a sequence has been consumed on Algorand

```bash
# BASE_EMITTER, WT_APP_ID, TRANSCEIVER_MGR_APP_ID must be set in environment
python utils/check_consumed.py <sequence>
python utils/check_consumed.py <sequence> --chain 30 --emitter <32-byte-hex>
```

Checks `vaas_consumed_` and `wormhole_peer_` boxes in `WormholeTransceiver`.

### Full diagnostic dump for a stuck sequence

```bash
# NTT_MGR_APP_ID, WT_APP_ID, TRANSCEIVER_MGR_APP_ID, BASE_EMITTER must be set
python utils/ntt_debug.py <sequence>
```

Fetches the VAA, parses every field, computes both digests, and queries all relevant on-chain boxes (`ntt_manager_peer_`, `num_attestations_`, `attestations_`, `messages_executed_`). Identifies exactly which step failed and why.

### Inspect an Algorand transaction

```bash
python utils/inspect_tx.py <TX_ID> [<TX_ID> ...]
```

Prints foreign apps, foreign assets, accounts, decoded app args, fees, and all inner transactions.

### SQLite state (Base→Algorand)

```python
import sqlite3
conn = sqlite3.connect('/opt/relayers/base_to_algo/.relay.db')
for row in conn.execute("SELECT sequence, status, attempts, last_error FROM sequences ORDER BY sequence"):
    print(row)
```

Status lifecycle: `pending → attested → complete | failed` (failed after 5 attempts).

---

## Contract migration (redeploy)

When Base NTT contracts are redeployed (addresses change):

**1. Update source files and .env**
```bash
# Set OLD_NTT, OLD_WT, NEW_NTT, NEW_WT before running
OLD_NTT=0x... OLD_WT=0x... NEW_NTT=0x... NEW_WT=0x... \
  bash utils/update_ntt_addresses.sh
```

**2. Update Algorand peer registry**
```bash
source base_to_algo/venv/bin/activate
python utils/update_algorand_peers.py <NEW_NTT_ADDRESS> <NEW_WT_ADDRESS>
```
This calls `setPeer` on the Algorand `NTT Manager` and `setWormholePeer` on the Algorand `WormholeTransceiver`, pointing them at the new Base addresses.

**3. Restart services**
```bash
sudo systemctl restart base-algo-relayer algo-base-relayer
```

---

## Technical reference

### VAA structure

```
byte 0       version (must be 1)
bytes 1-4    guardian_set_index (uint32 BE)
byte 5       num_signatures
bytes 6..    signatures: num_signatures × 66 bytes
               [0]    guardian index
               [1:66] ECDSA signature (r + s + v)
body start   6 + num_signatures × 66
  bytes 0-3  timestamp
  bytes 4-7  nonce
  bytes 8-9  emitter_chain (uint16 BE)
  bytes 10-41 emitter_address (32 bytes)
  bytes 42-49 sequence (uint64 BE)
  byte 50    consistency_level
  bytes 51+  payload
```

`vaa_digest = keccak256(keccak256(body))` — double-hash is the Wormhole standard.

### NTT payload layout (TransceiverMessage)

```
Outer payload (VAA body bytes 51+):
  [0:4]   0x9945ff10       TransceiverMessage prefix
  [4:36]  src_ntt          source NTT Manager (32-byte universal address)
  [36:68] dst_ntt          destination NTT Manager (32-byte universal address)
  [68:70] mplen            NttManagerPayload length (uint16 BE)
  [70..]  mp               NttManagerPayload

NttManagerPayload (mp):
  [0:32]  msg_id           message ID
  [32:64] user_univ        sender universal address
  [64:66] inner_len        NativeTokenTransfer length (uint16 BE)
  [66..]  inner_body       NativeTokenTransfer

NativeTokenTransfer (inner_body):
  [0:4]   0x994e5454       NTT transfer prefix
  [4]     from_decimals    source token decimals (uint8)
  [5:13]  from_amount      transfer amount (uint64 BE)
  [13:45] source_token     source token address (32 bytes)
  [45:77] recipient        recipient address (32 bytes — Algorand pubkey)
  [77:79] recipient_chain  destination Wormhole chain ID (uint16 BE — 8 = Algorand)
```

### Digest formulas

Two separate digests are computed from the same VAA:

**`ntt_digest`** — key for `TransceiverManager` attestation boxes (`num_attestations_`, `attestations_`):
```
keccak256(
    msg_id          (32 bytes, mp[0:32])
  + user_univ       (32 bytes, mp[32:64])
  + emitter_chain   (2 bytes, uint16 BE)
  + src_ntt         (32 bytes, outer p[4:36])
  + \x00*24 + NTT_MGR_APP_ID.to_bytes(8, 'big')   (32 bytes — app ID zero-padded)
  + mp[64:]         (inner: uint16 length prefix + NativeTokenTransfer)
)
```

**`msg_digest`** — key for `NTT Manager` executed messages box (`messages_executed_`):
```
keccak256(
    msg_id          (32 bytes)
  + user_univ       (32 bytes)
  + emitter_chain   (2 bytes)
  + src_ntt         (32 bytes)
  + dst_ntt         (32 bytes, outer p[36:68])
  + inner           (mp[64:], same as above)
)
```

The difference: `ntt_digest` uses `\x00*24 + NTT_MGR_APP_ID` as the handler address (the Algorand app ID zero-padded), while `msg_digest` uses `dst_ntt` (the universal address from the VAA). Both use the same Ethereum-style Keccak-256 (`0x01` padding, not NIST SHA3's `0x06`).

### Algorand box key formats

| Box | App | Key format |
|---|---|---|
| `wormhole_peer_` | WormholeTransceiver | `b"wormhole_peer_" + chain_id(2b)` |
| `vaas_consumed_` | WormholeTransceiver | `b"vaas_consumed_" + vaa_digest(32b)` |
| `handler_transceivers_` | TransceiverManager | `b"handler_transceivers_" + ntt_mgr_app_id(8b)` |
| `handler_paused_` | TransceiverManager | `b"handler_paused_" + ntt_mgr_app_id(8b)` |
| `num_attestations_` | TransceiverManager | `b"num_attestations_" + ntt_digest(32b)` |
| `attestations_` | TransceiverManager | `b"attestations_" + ntt_digest(32b) + wt_app_id(8b)` |
| `ntt_manager_peer_` | NTT Manager | `b"ntt_manager_peer_" + chain_id(2b)` |
| `messages_executed_` | NTT Manager | `b"messages_executed_" + msg_digest(32b)` |

### Wormhole Core LogicSig (vaa_verify.teal)

Guardian signature verification on Algorand uses a template LogicSig program. The relayer patches four placeholders into the compiled bytecode before each use:

| Template var | Encoding | Purpose |
|---|---|---|
| `TMPL_ADDR_IDX` | uvarint | Index of this LogicSig in the guardian set |
| `TMPL_EMITTER_ID` | length-prefixed bytes | Emitter address from the VAA |
| `TMPL_APP_ID` | uvarint | Wormhole Core app ID |
| `TMPL_APP_ADDRESS` | length-prefixed bytes | Wormhole Core app address |

`vaa_verify.teal` is not included in this repo — `setup.sh` downloads it from the Wormhole GitHub repository. The relayer looks for it in the following order:

1. Same directory as `base-relayer.py` (e.g. `/opt/relayers/base_to_algo/vaa_verify.teal`) — **persistent across reboots**
2. `/tmp/wormhole/algorand/teal/vaa_verify.teal` — fallback, lost on reboot

Copy it to the relayer directory to ensure it survives server restarts:
```bash
cp /path/to/wormhole/algorand/teal/vaa_verify.teal /opt/relayers/base_to_algo/vaa_verify.teal
```

The compiled output is cached at `/tmp/vaa_verify_program.b64` and rebuilt automatically if missing.

---

## Security

### Secrets

All credentials are loaded exclusively from environment variables or a `.env` file — never hardcoded. The required secrets are:

| Secret | Service | Purpose |
|---|---|---|
| `PRIVATEKEY` | Algo → Base | Base wallet private key (64 hex chars, no `0x` prefix) |
| `ALGO_MNEMONIC` | Base → Algo | 25-word Algorand mnemonic for the relayer wallet |

The `.env` file should be readable only by the service user:
```bash
chmod 600 /opt/relayers/algo_to_base/.env
chmod 600 /opt/relayers/base_to_algo/.env
```

### Private key handling (Algorand → Base)

`cast_relayer.py` loads the Base private key from the `.env` file at startup via `python-dotenv` and passes it to `cast send` via `--private-key`. The key is never hardcoded in source — it lives only in the `.env` file on the server, which should be `chmod 600`.

`utils/relay-once.sh` is a manual one-shot debug script that also uses `--private-key` — it should only be used in trusted environments and not run as part of the production service.

### SQL injection protection

`relay_service.py` validates all column names passed to `db_upsert()` against a static allowlist (`_ALLOWED_COLS`) before constructing any SQL. Unknown column names raise `ValueError` immediately.

### VAA payload bounds checking

`base-relayer.py` and `ntt_execute.py` validate payload length at each field boundary before slicing. Truncated or malformed VAAs are rejected before any on-chain transaction is submitted.

### Replay protection

- **Algorand → Base**: SQLite keyed on `sequence` — already-relayed sequences are skipped on poll.
- **Base → Algorand**: SQLite state machine (`pending → attested → complete`). The Algorand `WormholeTransceiver` also maintains a `vaas_consumed_` box on-chain; a VAA whose digest is already in that box will be rejected at the contract level even if the DB is cleared.

### TEAL files

No `.teal` files are committed to this repository. `vaa_verify.teal` is obtained separately (via `setup.sh` or manual copy) and never checked in, as it is part of the public Wormhole SDK and contains no secrets.

---

## Contract addresses

All contract addresses and app IDs are deployment-specific. Set them in `.env` — see `.env.example` for the full list of required variables. The only hardcoded value in the source is the Wormhole Core app ID (`842125965`), which is the public shared Wormhole infrastructure contract on Algorand mainnet and is not expected to change.

---

## Database schemas

**`algo_to_base/.relayer.db`** (Algorand→Base)
```sql
CREATE TABLE relayed (
    sequence  TEXT PRIMARY KEY,   -- Wormhole sequence number
    txHash    TEXT,               -- Base transaction hash (0x-prefixed)
    relayedAt INTEGER             -- Unix timestamp
);
```

**`base_to_algo/.relay.db`** (Base→Algorand)
```sql
CREATE TABLE sequences (
    sequence      INTEGER PRIMARY KEY,
    status        TEXT NOT NULL DEFAULT 'pending',
    vaa_digest    TEXT,
    ntt_digest    TEXT,
    recipient     TEXT,
    amount        INTEGER,
    attest_txid   TEXT,
    execute_txid  TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
```
