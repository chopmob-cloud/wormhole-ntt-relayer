#!/usr/bin/env bash
# Base → Algorand NTT Relay — server setup
# Run from the base_to_algo/ directory: bash setup.sh
set -euo pipefail

RELAY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$RELAY_DIR/venv"
SERVICE_NAME="base-algo-relayer"

echo "════════════════════════════════════════"
echo " Base → Algorand NTT Relay Setup"
echo " Install dir: $RELAY_DIR"
echo "════════════════════════════════════════"

# ── 1. System deps ────────────────────────────────────────────────────────────
echo ""
echo "── [1/5] System dependencies ──"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-pip python3-venv curl

# ── 2. Python venv ────────────────────────────────────────────────────────────
echo ""
echo "── [2/5] Python venv ──"
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet -r "$RELAY_DIR/requirements.txt"
echo "  Installed: $(pip show py-algorand-sdk pycryptodome requests | grep -E '^(Name|Version):' | paste - - | awk '{print $2 " " $4}' | tr '\n' '  ')"
deactivate

# ── 3. .env file ──────────────────────────────────────────────────────────────
echo ""
echo "── [3/5] Environment config ──"
ENV_FILE="$RELAY_DIR/.env"

if [ -f "$ENV_FILE" ]; then
    echo "  .env already exists — skipping"
else
    cat > "$ENV_FILE" << 'EOF'
# ── Algorand account ──────────────────────────────────────────────────────────
ALGO_MNEMONIC=

# ── Algorand node ─────────────────────────────────────────────────────────────
ALGOD_URL=https://mainnet-api.4160.nodely.dev
ALGOD_TOKEN=

# ── Source chain emitter (Base WormholeTransceiver, 32-byte hex, no 0x) ───────
BASE_EMITTER=

# ── Algorand App IDs (required — set from your NTT deployment) ────────────────
WT_APP_ID=
NTT_MGR_APP_ID=
TRANSCEIVER_MGR_APP_ID=
NTT_TOKEN_ASSET_ID=

# ── Optional overrides ────────────────────────────────────────────────────────
# WORMHOLE_CORE_APP_ID=842125965
EOF
    chmod 600 "$ENV_FILE"
    echo "  Created $ENV_FILE"
    echo "  ⚠️  Edit $ENV_FILE and set ALGO_MNEMONIC before starting!"
fi

# ── 4. vaa_verify program ─────────────────────────────────────────────────────
echo ""
echo "── [4/5] vaa_verify TEAL program ──"
TEAL_DIR="/tmp/wormhole/algorand/teal"
TEAL_PATH="$TEAL_DIR/vaa_verify.teal"
VAA_CACHE="/tmp/vaa_verify_program.b64"

mkdir -p "$TEAL_DIR"

if [ -f "$VAA_CACHE" ]; then
    echo "  Compiled cache already present at $VAA_CACHE"
elif [ -f "$TEAL_PATH" ]; then
    echo "  vaa_verify.teal already present — will compile on first run"
else
    TEAL_URL="https://raw.githubusercontent.com/wormhole-foundation/wormhole/main/algorand/teal/vaa_verify.teal"
    echo "  Downloading from Wormhole repo..."
    if curl -sfL "$TEAL_URL" -o "$TEAL_PATH"; then
        echo "  ✓ Downloaded to $TEAL_PATH"
        echo "  Will be compiled and cached on first relay run"
    else
        echo "  ✗ Download failed. Copy from an existing relay server:"
        echo "    scp ubuntu@<server>:/tmp/vaa_verify_program.b64 /tmp/"
        echo "  Or from the Wormhole repo at:"
        echo "    https://github.com/wormhole-foundation/wormhole/blob/main/algorand/teal/vaa_verify.teal"
    fi
fi

# ── 5. Systemd service ────────────────────────────────────────────────────────
echo ""
echo "── [5/5] Systemd service ──"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

sudo tee "$SERVICE_FILE" > /dev/null << EOF
[Unit]
Description=Base -> Algorand NTT Relay Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$RELAY_DIR
Environment=ENV_FILE=$RELAY_DIR/.env
Environment=POLL_INTERVAL=30
ExecStart=$VENV_DIR/bin/python $RELAY_DIR/relay_service.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
echo "  Service installed and enabled: $SERVICE_NAME"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════"
echo " Setup complete!"
echo "════════════════════════════════════════"
echo ""
echo " Next steps:"
echo ""
echo " 1. Set your mnemonic:"
echo "    nano $ENV_FILE"
echo ""
echo " 2. Test a single sequence before starting the service:"
echo "    source $VENV_DIR/bin/activate"
echo "    python $RELAY_DIR/base-relayer.py <seq> --dry-run"
echo "    python $RELAY_DIR/ntt_execute.py <seq> --dry-run"
echo ""
echo " 3. Start the service:"
echo "    sudo systemctl start $SERVICE_NAME"
echo "    sudo journalctl -u $SERVICE_NAME -f"
echo ""
echo " Useful commands:"
echo "   sudo systemctl status $SERVICE_NAME"
echo "   sudo systemctl restart $SERVICE_NAME"
echo "   sudo journalctl -u $SERVICE_NAME -f"
