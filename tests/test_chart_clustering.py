"""chart-clustering's own tests (chart-clustering/tests/), run with the repo's suite."""
import importlib.util
from pathlib import Path

_path = Path(__file__).resolve().parents[1] / "chart-clustering" / "tests" / "test_chart_clustering.py"
_spec = importlib.util.spec_from_file_location("chart_clustering_tests", _path)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
globals().update({k: v for k, v in vars(_module).items() if k.startswith(("test_", "_"))
                  and not k.startswith("__")})
