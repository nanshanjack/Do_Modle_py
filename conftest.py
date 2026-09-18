"""确保项目根目录在 sys.path 上，使 ``import do_modle`` 在任意 cwd 下都可用。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
