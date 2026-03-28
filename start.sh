#!/bin/bash
# ClosePro MVP — Quick Start Script

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   ClosePro MVP — Starting...             ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# Check Python 3
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 is required. Install it from https://python.org"
    exit 1
fi

# Install only what's missing (no venv needed — uses system Python)
echo "📦 Checking dependencies..."
python3 -c "import tornado" 2>/dev/null || pip3 install tornado --break-system-packages -q
python3 -c "import jwt"     2>/dev/null || pip3 install PyJWT    --break-system-packages -q

echo "✓ Dependencies ready"
echo ""

# Optional: AI integrations
echo "💡 AI Key status:"
if [ -f ".env" ]; then
    grep -q "OPENAI_API_KEY=sk-" .env     && echo "  ✓ OpenAI key found"     || echo "  – No OpenAI key (using smart fallback)"
    grep -q "ANTHROPIC_API_KEY=sk-" .env  && echo "  ✓ Anthropic key found"  || echo "  – No Anthropic key"
else
    echo "  – No .env file (copy .env.example to .env to add AI keys)"
fi
echo ""

# Start
python3 app.py
