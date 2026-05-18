"""pytest 루트 conftest — worktree 의 src/ 를 sys.path 앞에 추가한다.

editable install (parent project) 이 site-packages 에 잡혀 있는 경우, 워크트리
경로의 신규 모듈(예: ``context_loop.eval`` 신규 서브패키지)을 인식하지 못한다.
워크트리 src/ 를 sys.path 첫 번째로 두어 pytest collection 이 워크트리 코드를
우선 로드하도록 한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

_WORKTREE_SRC = Path(__file__).resolve().parent / "src"
if _WORKTREE_SRC.is_dir() and str(_WORKTREE_SRC) not in sys.path:
    sys.path.insert(0, str(_WORKTREE_SRC))
