import sys
from pathlib import Path

# So `import lib.nodeset...` resolves regardless of pytest's rootdir/import-mode
# behavior — keeps this independently runnable without a packaging step.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "nodesets"
