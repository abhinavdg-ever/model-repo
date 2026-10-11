#!/usr/bin/env bash
# Set up BERT training on a Mac or Linux PC. Run from this folder:
#
#   ./setup_pc.sh              # the default PyTorch wheels (CUDA on Linux with an NVIDIA GPU, CPU on a Mac)
#   ./setup_pc.sh --notebook   # also install Jupyter, to run pc.ipynb
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3.12}"
command -v "$PYTHON" >/dev/null || { echo "Python 3.12 not found (macOS: brew install python@3.12; Linux: apt install python3.12 python3.12-venv)"; exit 1; }

echo "== Python venv (.venv)"
[ -x .venv/bin/python ] || "$PYTHON" -m venv .venv
PY=.venv/bin/python
"$PY" -m pip install --upgrade pip >/dev/null

echo "== PyTorch + the rest (requirements.txt)"
"$PY" -m pip install -r requirements.txt
if [ "${1:-}" = "--notebook" ]; then "$PY" -m pip install notebook ipykernel ipywidgets; fi

echo "== Check"
"$PY" -c "import torch, transformers; print('torch', torch.__version__, '| transformers', transformers.__version__); print('GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none - training will use the CPU')"
echo
echo "Done. Next:"
echo "  source .venv/bin/activate"
echo "  python train.py --check        # 2 steps + minutes-per-epoch estimate"
echo "  python train.py                # a new run: 4 epochs"
[ "${1:-}" = "--notebook" ] && echo "  jupyter notebook pc.ipynb      # or open pc.ipynb in VS Code"
exit 0
