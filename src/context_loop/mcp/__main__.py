"""``python -m context_loop.mcp`` 실행 진입점.

콘솔 스크립트(``context-loop mcp serve``)와 동일하게 MCP 서버를 띄운다.
인자 없이 실행하면 stdio 전송으로 동작한다.
"""

from __future__ import annotations

from context_loop.cli import main

if __name__ == "__main__":
    # "python -m context_loop.mcp [serve ...]" 형태로 받은 인자를
    # CLI 의 "mcp" 서브커맨드로 위임한다. 인자가 없으면 "serve" 로 기본 동작.
    import sys

    argv = sys.argv[1:] or ["serve"]
    raise SystemExit(main(["mcp", *argv]))
