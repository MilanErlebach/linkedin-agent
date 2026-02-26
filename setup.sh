#!/bin/bash
# LinkedIn Agent Setup Script
# Erstellt virtualenv und installiert Python-Abhängigkeiten

set -e

AGENT_DIR="/opt/linkedin-agent"
VENV_DIR="$AGENT_DIR/venv"
PYTHON="/usr/bin/python3"

echo "=== LinkedIn Agent Setup ==="
echo ""

# Check Python
if ! command -v $PYTHON &> /dev/null; then
    echo "ERROR: Python3 nicht gefunden unter $PYTHON"
    exit 1
fi
echo "✓ Python: $($PYTHON --version)"

# Create virtualenv
if [ -d "$VENV_DIR" ]; then
    echo "ℹ  Virtualenv existiert bereits, überspringe Erstellung"
else
    echo "→ Erstelle virtualenv..."
    $PYTHON -m venv "$VENV_DIR"
    echo "✓ Virtualenv erstellt: $VENV_DIR"
fi

# Install dependencies
echo "→ Installiere Python-Pakete..."
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install -r "$AGENT_DIR/agent/requirements.txt" --quiet
echo "✓ Pakete installiert"

# Make scripts executable
chmod +x "$AGENT_DIR/agent/main.py"
chmod +x "$AGENT_DIR/agent/post_generator.py"
echo "✓ Skripte executable gemacht"

# Create .env if not exists
if [ ! -f "$AGENT_DIR/.env" ]; then
    cp "$AGENT_DIR/.env.example" "$AGENT_DIR/.env"
    echo ""
    echo "⚠️  WICHTIG: Bitte befülle jetzt die Credentials:"
    echo "   nano $AGENT_DIR/.env"
else
    echo "✓ .env existiert bereits"
fi

echo ""
echo "=== Setup abgeschlossen ==="
echo ""
echo "Nächste Schritte:"
echo "1. Anthropic API Key holen: https://console.anthropic.com → API Keys"
echo "2. Gmail App-Passwort erstellen: Google Account → Sicherheit → App-Passwörter"
echo "3. .env befüllen: nano $AGENT_DIR/.env"
echo "4. Test: $VENV_DIR/bin/python3 --version"
echo ""
echo "Test-Befehl (nach .env befüllen):"
echo "  echo '{\"email_content\":\"Test\",\"email_subject\":\"Test\",\"rss_openai\":[],\"rss_anthropic\":[]}' > /tmp/test.json"
echo "  $VENV_DIR/bin/python3 $AGENT_DIR/agent/main.py /tmp/test.json"
