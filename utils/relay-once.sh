#!/usr/bin/env bash
set -euo pipefail

# Manually relay one Algorand->Base VAA via cast.
# Usage: ./relay-once.sh <SEQ> [--env /path/to/.env]

SEQ="${1:-}"
ENV_FILE="${ENV_FILE:-}"

shift 1 2>/dev/null || true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env) ENV_FILE="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

if [ -z "$SEQ" ]; then
  echo "Usage: $0 <sequence-number> [--env /path/to/.env]"
  exit 1
fi

# Locate .env if not specified
if [ -z "$ENV_FILE" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  for candidate in \
    "$SCRIPT_DIR/../algo_to_base/.env" \
    "$SCRIPT_DIR/.env" \
    "$(pwd)/.env"
  do
    if [ -f "$candidate" ]; then
      ENV_FILE="$candidate"
      break
    fi
  done
fi

if [ -z "$ENV_FILE" ] || [ ! -f "$ENV_FILE" ]; then
  echo "Error: .env not found. Set ENV_FILE= or pass --env /path/to/.env"
  echo "       Required vars: PRIVATE_KEY, BASE_TRANSCEIVER, ALGO_EMITTER"
  echo "       Optional vars: RPC_URL"
  exit 1
fi

echo "[env] $ENV_FILE"
# shellcheck source=/dev/null
set -a; source "$ENV_FILE"; set +a

PRIVATE_KEY="${PRIVATE_KEY:?PRIVATE_KEY not set in $ENV_FILE}"
TRANSCEIVER="${BASE_TRANSCEIVER:?BASE_TRANSCEIVER not set in $ENV_FILE}"
RPC="${RPC_URL:-https://mainnet.base.org}"

CHAIN=8
EMITTER="${ALGO_EMITTER:?ALGO_EMITTER not set in $ENV_FILE}"

echo "[1] Fetching VAA base64 for seq $SEQ (chain $CHAIN)..."
curl -s "https://api.wormholescan.io/api/v1/vaas/${CHAIN}/${EMITTER}/${SEQ}" \
  | jq -r '.data.vaa' > vaa.base64

if ! [ -s vaa.base64 ]; then
  echo "Error: empty response — VAA not yet available for seq $SEQ"
  exit 1
fi

echo "[2] Converting VAA base64 -> hex..."
node - << 'EOF'
const fs = require('fs');
const b64 = fs.readFileSync('vaa.base64', 'utf8').trim();
if (!b64) { console.error('vaa.base64 is empty'); process.exit(1); }
fs.writeFileSync('vaa.hex', '0x' + Buffer.from(b64, 'base64').toString('hex'));
console.log('vaa.hex written');
EOF

VAA=$(cat vaa.hex)

echo "[3] Sending VAA to $TRANSCEIVER on Base..."
cast send "$TRANSCEIVER" \
  "receiveMessage(bytes)" "$VAA" \
  --private-key "$PRIVATE_KEY" \
  --rpc-url "$RPC" \
  --gas-limit 500000
