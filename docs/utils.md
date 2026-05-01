# utils/

Diagnostic, maintenance, and operational tools. None of these run as services — they are invoked manually when needed.

---

## ntt_digest.py

### Purpose

Standalone utility that computes the **NTT message digest** — the keccak256 hash used as the key for `num_attestations_<digest>` and `attestations_<digest><wt_app>` boxes in TransceiverManager. Includes a full pure-Python Keccak-256 implementation and a self-test with a known mainnet VAA.

Use when: you need to manually compute a digest to check box state without running `ntt_debug.py`, or to verify the formula is correct for a new sequence.

### Layout

```
Pure Python Keccak-256
  _rot64(x, n)          64-bit left rotation
  _KECCAK_RC            24 round constants
  _KECCAK_ROT           rotation offsets table
  _keccak_f(state)      Keccak-f[1600] permutation
  keccak256(data)       absorb-and-squeeze producing 32 bytes
    NOTE: uses 0x01 padding suffix (Ethereum Keccak), NOT 0x06 (NIST SHA3)
    Verified against known test vector: keccak256(b"") == c5d2460186f7...

NTT digest formula
  compute_ntt_message_digest(source_chain_id, source_ntt_manager,
                             handler_app_id, ntt_manager_payload)
    preimage = msg_id(32) + user_address(32) + source_chain(2)
             + source_ntt_manager(32) + handler_address(32) + payload(var)
    handler_address = b"\x00"*24 + app_id.to_bytes(8, "big")
    payload = ntt_manager_payload[64:]  (length prefix + NativeTokenTransfer)
    returns keccak256(preimage)

  parse_transceiver_message(vaa_payload)
    Parses TransceiverMessage from raw VAA payload bytes:
      [0:4]    prefix (9945FF10)
      [4:36]   source_ntt_manager
      [36:68]  recipient_ntt_manager
      [68:70]  ntt_manager_payload_length
      [70..]   ntt_manager_payload
      [70+N..] transceiver_payload_length + transceiver_payload

  get_receive_message_box_refs(wt_app_id, tm_app_id, source_chain_id,
                               ntt_manager_digest, vaa_digest)
    Returns list of (app_id, box_name) tuples for receive_message call.

Self-test (__main__)
  Verifies keccak256(b"") against the standard empty-input test vector.
  Prints a usage comment showing how to call compute_ntt_message_digest.
```

### How it was developed

Written on **Mar 2, 2026** as a dedicated research artifact after `base_relayer.py` kept producing wrong box keys. The formula was reverse-engineered from the Folks Finance PuyaPy implementation of `calculate_message_digest` by tracing the field ordering through the PuyaPy ABI encoding.

A pure Python Keccak-256 was written from scratch (rather than using `pycryptodome`) because the intent was to make this file fully self-contained and runnable without any installation — useful when SSHed onto the server without a venv. The `hashlib.sha3_256` stdlib function could not be used because Python's SHA3 uses the NIST padding byte (`0x06`) while the Ethereum/Wormhole ecosystem uses the original Keccak padding (`0x01`).

---

## ntt_debug.py

### Purpose

Full diagnostic tool for a single sequence. Dumps every parsed field of the VAA's NTT payload, computes both digests, and queries on-chain Algorand box state to show exactly where in the pipeline a given sequence is stalled.

Use when: a sequence is failing and you need to understand why — wrong chain, wrong recipient, attestation not recorded, already executed, field parsing mismatch.

```
python3 ntt_debug.py <sequence>
```

### Layout

```
Config
  ALGOD_URL, NTT_MGR, TRANSCEIVER_MGR, WT_APP, EMITTER, CHAIN, SIG_LEN

Helpers
  keccak256(data)     pycryptodome Keccak-256
  parse_vaa(vaa_bytes)  extracts emitter_chain, vaa_digest, payload
  hex_block(label, data)  prints hex + ASCII decode attempt
  fetch_box(app_id, name)  GET /v2/applications/{app_id}/box — returns bytes or None

Main (main())
  1. Fetch VAA from WormholeScan for given sequence
  2. Parse VAA, print emitter_chain and vaa_digest
  3. Print outer payload first 80 bytes
  4. Verify NTT prefix (9945ff10)
  5. Parse and print all outer payload fields (src_ntt, dst_ntt, mplen)
  6. Parse mp: print all 4 sub-fields with hex_block
  7. Decode inner_body: from_decimals, from_amount, source_token, recipient, recipient_chain
     - Flags recipient_chain != 8 as an error
  8. Compute and print ntt_digest (TRANSCEIVER_MGR key)
  9. Compute and print msg_digest (NTT_MGR execute key)
  10. Expected handler_univ check: zero-pad + NTT_MGR.to_bytes(8)
      - Flags mismatch (execute_message will assert-fail)
  11. On-chain peer box check: ntt_manager_peer_<chain> on NTT_MGR
      - Compares peer_contract vs src_ntt
  12. On-chain TRANSCEIVER_MGR attestation boxes:
      num_attestations_<ntt_digest> — exists and count
      attestations_<ntt_digest><WT_APP> — exists
  13. NTT_MGR execution state:
      messages_executed_<msg_digest> — already executed?
      rate_limit_buckets_<ntt_digest> — rate limit state
```

### How it was developed

Written around **Mar 2-3, 2026** as sequences 9–11 were failing `execute_message`. The tool was built incrementally as each new failure mode was encountered — each failed `execute_message` call added a new section to the diagnostic output. The `handler_address check` section was added specifically after a mismatch between `ntt_digest` and `msg_digest` formulas caused repeated assertion failures that were hard to diagnose from logs alone.

---

## check_consumed.py

### Purpose

CLI tool to check whether a Base→Algorand NTT VAA has been consumed on Algorand. Fetches the VAA from WormholeScan dynamically, computes the vaa_digest, and queries the on-chain box state.

```bash
python check_consumed.py <sequence>
python check_consumed.py <sequence> --chain 30 --emitter 00000000...65a7...
```

Use when: you need a fast check on a specific sequence without running the full `ntt_debug.py`.

### Layout

```
Config
  WORMHOLESCAN  WormholeScan API base URL
  ALGOD         mainnet-api.algonode.cloud
  WT_APP        from env WT_APP_ID
  TRANSCEIVER_MGR  from env TRANSCEIVER_MGR_APP_ID
  DEFAULT_CHAIN   30 (Base), overridable via --chain
  DEFAULT_EMITTER  from env BASE_EMITTER, overridable via --emitter

Helpers
  keccak256(data)        pycryptodome Keccak-256
  box_exists(app_id, name_bytes)   GET /v2/applications/{app_id}/box — returns bool
  fetch_vaa(chain, emitter, seq)   fetches and base64-decodes VAA from WormholeScan
  parse_vaa_body(vaa)   strips version + guardian sigs, returns body bytes

main()
  1. Parse args: sequence, --chain, --emitter
  2. Fetch VAA from WormholeScan
  3. Compute vaa_digest = keccak256(body)
  4. Print VAA body fields (emitter_chain, emitter_addr, sequence)
  5. Check vaas_consumed_<vaa_digest> in WT_APP — was Step 1 relayed?
  6. Check wormhole_peer_<chain_2b><emitter_32b> in WT_APP — is peer configured?
  7. Print result and recommended next step
```

### How it was developed

Originally written on **Mar 2, 2026** with hardcoded digests from sequence 2 — used as a quick sanity check during active debugging. Refactored to accept CLI args and fetch the VAA dynamically so it works for any sequence without editing the file.

---

## inspect_tx.py

### Purpose

Queries the Algorand Indexer for one or more transaction IDs and prints every parameter — foreign apps, foreign assets, accounts, decoded app args, fees, inner transactions. Used during development to extract the exact parameters needed for the relay transaction group by examining known-good on-chain transfers.

```bash
python inspect_tx.py <TX_ID> [<TX_ID> ...]
```

### Layout

```
Config
  INDEXER   "https://mainnet-idx.algonode.cloud"

inspect_tx(tx_id)
  GET /v2/transactions/{tx_id}
  Prints for each:
    tx-type
    application-transaction:
      application-id, foreign-apps, foreign-assets, accounts
      app-args (decoded to hex + length)
      on-completion
    payment-transaction: receiver, amount
    asset-transfer-transaction: asset-id, amount, receiver
    fee
    inner-txns (up to any depth): type + key fields

main()
  argparse: tx_ids (one or more positional args)
  Calls inspect_tx() for each
```

### How it was developed

Written once to decode the 4 transactions from the first successful `transfer.ts` run — the NTT deployment tooling's working example. By inspecting those transactions, the exact `foreign_apps`, `boxes`, and `accounts` lists needed for `base-relayer.py` were extracted. Refactored to accept CLI args so it can be used to inspect any transaction without editing the file.

---

## update_ntt_addresses.sh

### Purpose

Migration script for when NTT contract addresses change after a redeploy. Performs a case-insensitive sed replacement of old addresses across all Python source files and `.env` files in one pass, then reports remaining references and verifies new addresses are present.

```bash
./update_ntt_addresses.sh
```

### Layout

```
Variables
  OLD_NTT / NEW_NTT   old and new Base NTT Manager addresses
  OLD_WT  / NEW_WT    old and new Base WormholeTransceiver addresses

Step 1 — Python files
  RELAYER_FILES list: cast_relayer.py, relay.py, relay_seq_2.py, etc.
  For each file: 4 sed -i passes covering checksummed, lowercase, and
  bare-hex variants of both addresses.

Step 2 — .env files
  Same sed passes on .env and ~/.env

Step 3 — Scan for remaining old references
  grep -rl for the first 6 chars of old addresses (b4254f, 5865af)
  excluding .pyc, __pycache__, .git

Step 4 — Verify new addresses present
  grep -ic for new address fragments in the main relayer files
  Reports count per file.

Summary
  Prints new addresses with Basescan links
  Lists next steps: approve token spending, restart services
```

### How it was developed

Written after the NTT contracts were redeployed with updated addresses. The `.backup.20260301` files on the production server are snapshots taken the day before the migration ran.

---

## update_algorand_peers.py

### Purpose

Updates the peer configuration on the Algorand WormholeTransceiver — registers or updates the Base (chain 30) peer address. Run after a contract redeploy changes the Base NTT Manager address that Algorand needs to recognise.

### Layout

```
Config
  app IDs: WORMHOLE_TRANSCEIVER, NTT_MANAGER, TRANSCEIVER_MANAGER
  peer chain + address values
  algod client setup
  mnemonic loading from env

Transaction building
  ApplicationCallTxn to WormholeTransceiver with setPeer or equivalent method
  Box references for the peer entry
  Signs and submits via algod
```

### How it was developed

Written alongside `update_ntt_addresses.sh` as the Algorand-side counterpart to the address migration. When Base contracts are redeployed, the Base-side needs `update_ntt_addresses.sh` to update Python source, and the Algorand-side needs `update_algorand_peers.py` to update the on-chain peer registry.

---

## relay-once.sh

### Purpose

Manually relays a single Algorand→Base sequence using `cast send`. Useful for backfilling a missed sequence without starting the full polling service, or for testing the Base contract call in isolation.

```bash
./relay-once.sh <sequence>
./relay-once.sh <sequence> --env /path/to/.env
ENV_FILE=/path/to/.env ./relay-once.sh <sequence>
```

### Layout

```
Arg parsing
  SEQ     positional (required)
  --env   optional override for .env path

.env location
  Searches in order: ../algo_to_base/.env, ./  .env, $(pwd)/.env
  source "$ENV_FILE" (set -a / set +a) — loads all vars into environment

Required env var
  PRIVATE_KEY   Base wallet private key (0x-prefixed hex)

Optional env vars (with defaults)
  TRANSCEIVER   from env BASE_TRANSCEIVER (required)
  RPC_URL       https://mainnet.base.org

Constants
  CHAIN     8 (Algorand)
  EMITTER   Algorand NTT emitter (base32)

Step 1
  curl WormholeScan /vaas/{CHAIN}/{EMITTER}/{SEQ} | jq -r .data.vaa > vaa.base64
  Exits if vaa.base64 is empty.

Step 2
  node inline script: base64 → 0x-hex → vaa.hex

Step 3
  cast send TRANSCEIVER "receiveMessage(bytes)" VAA_HEX
    --private-key $PRIVATE_KEY --rpc-url $RPC --gas-limit 500000
```

### How it was developed

This is the **oldest file in the repo** (Feb 23, 2026 — a week before the Python relayers). It was the first proof that the Base WormholeTransceiver contract call worked at all. Originally had hardcoded private key and old pre-migration contract addresses. Updated to load credentials from `.env` and default to the current contract addresses.
