#!/bin/bash
set -e

# Quiet uv output when not attached to a terminal (e.g. launched by the Python launcher)
if [ -t 1 ]; then
    QUIET=""
else
    QUIET="--quiet"
fi

# Detect OS and select appropriate requirements file
if [[ "$OSTYPE" == "darwin"* ]]; then
    REQUIREMENTS_FILE="requirements.macos.txt"
    echo "Detected macOS, using $REQUIREMENTS_FILE"
elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    REQUIREMENTS_FILE="requirements.ubuntu.txt"
    echo "Detected Linux, using $REQUIREMENTS_FILE"
else
    echo "Unsupported OS: $OSTYPE"
    exit 1
fi

# Check if uv is installed
if ! command -v uv &> /dev/null; then
    echo "uv is not installed. Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # Source the shell profile to make uv available
    export PATH="$HOME/.local/bin:$PATH"
fi

echo "Creating virtual environment with Python 3.11..."
uv venv $QUIET --python 3.11 --allow-existing

# Blackwell (RTX 50xx, sm_120) needs the CUDA-12.8 nightly torch; stable builds
# top out at sm_90 and fall back to CPU. Detect it before installing so we can
# keep the nightly out of the requirements re-resolve below.
GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || echo "")
IS_BLACKWELL=0
if echo "${GPU_NAME}" | grep -qi "RTX 50\|B200\|B100\|blackwell"; then
    IS_BLACKWELL=1
fi

# The requirements file pins a stable torch that would clobber an already-installed
# cu128 nightly on every run (a multi-GB reinstall). Once the nightly is in place
# (its version carries a +cu128 local label that stable PyPI wheels lack), hold
# torch out of the re-resolve and skip the reinstall entirely.
TORCH_IS_CU128=0
if [ "$IS_BLACKWELL" = "1" ] && \
   .venv/bin/python -c "import torch,sys; sys.exit(0 if '+cu128' in torch.__version__ else 1)" 2>/dev/null; then
    TORCH_IS_CU128=1
fi

echo "Installing dependencies from $REQUIREMENTS_FILE..."
if [ "$TORCH_IS_CU128" = "1" ]; then
    echo "✅ PyTorch cu128 nightly already installed - keeping it out of the requirements re-resolve"
    TMP_REQS=$(mktemp)
    grep -v '^torch==' "$REQUIREMENTS_FILE" > "$TMP_REQS"
    uv pip install $QUIET -r "$TMP_REQS" --python .venv/bin/python
    rm -f "$TMP_REQS"
else
    uv pip install $QUIET -r "$REQUIREMENTS_FILE" --python .venv/bin/python
fi

# Install the CUDA-12.8 nightly torch on first setup (or after a venv reset).
if [ "$IS_BLACKWELL" = "1" ] && [ "$TORCH_IS_CU128" = "0" ]; then
    echo "⚡ Blackwell GPU detected (${GPU_NAME}) - installing PyTorch nightly cu128..."
    uv pip install $QUIET --python .venv/bin/python --pre --reinstall torch \
        --index-url https://download.pytorch.org/whl/nightly/cu128
    # Pin numpy to <2.3 (numba compatibility)
    uv pip install $QUIET --python .venv/bin/python "numpy<2.3"
    echo "✅ PyTorch nightly cu128 installed"
fi

echo ""
echo "Setup complete! Activate the environment with:"
echo "  source .venv/bin/activate"
echo ""
echo "NOTE: You also need to download the ReplicaCAD datasets."
echo "See data/README.md for download instructions."
