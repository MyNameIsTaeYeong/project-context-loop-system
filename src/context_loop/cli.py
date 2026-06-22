"""context-loop CLI 진입점.

``pyproject.toml`` 의 ``[project.scripts]`` 에 등록된 ``context-loop`` 명령의
구현부다. 현재는 MCP 서버 실행 서브커맨드를 제공한다.

사용 예::

    context-loop mcp serve                 # stdio 전송 (Claude Code 등 로컬 연동)
    context-loop mcp serve --transport sse # SSE 전송 (원격/팀 공유)
    context-loop mcp serve --transport sse --port 3001
    context-loop mcp serve --transport sse --host 0.0.0.0 --port 3001
                                           # 사내 LAN 의 다른 PC 에서 접속 허용
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

logger = logging.getLogger(__name__)


def _default_transport_port_host() -> tuple[str, int, str]:
    """설정 파일에서 MCP 전송 방식·포트·바인딩 호스트 기본값을 읽는다.

    설정 로드 실패 시에도 CLI 는 동작해야 하므로 안전한 기본값으로 폴백한다.
    """
    try:
        from context_loop.config import Config

        config = Config()
        transport = config.get("mcp.transport", "stdio")
        port = int(config.get("mcp.sse_port", 3001))
        host = str(config.get("mcp.sse_host", "127.0.0.1"))
        return transport, port, host
    except Exception:  # 설정 로드 실패 시에도 CLI 는 동작해야 한다
        return "stdio", 3001, "127.0.0.1"


def _build_parser() -> argparse.ArgumentParser:
    default_transport, default_port, default_host = _default_transport_port_host()

    parser = argparse.ArgumentParser(
        prog="context-loop",
        description="사내 지식 Context Loop System CLI",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    mcp_parser = subcommands.add_parser("mcp", help="MCP 서버 관련 명령")
    mcp_sub = mcp_parser.add_subparsers(dest="mcp_command", required=True)

    serve_parser = mcp_sub.add_parser("serve", help="MCP 서버를 실행한다")
    serve_parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default=default_transport,
        help=f"전송 방식 (기본값: {default_transport})",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=default_port,
        help=f"SSE 전송 시 포트 (기본값: {default_port})",
    )
    serve_parser.add_argument(
        "--host",
        default=default_host,
        help=(
            "SSE 전송 시 바인딩할 인터페이스 "
            f"(기본값: {default_host}). 사내 LAN 의 다른 PC 에서 접속하려면 "
            "0.0.0.0 을 지정한다."
        ),
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 진입점. 반환값은 프로세스 종료 코드."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "mcp" and args.mcp_command == "serve":
        # 지연 import: 무거운 의존성(서버/저장소)은 실제 실행 시에만 로드
        from context_loop.mcp.server import run_sse, run_stdio

        if args.transport == "sse":
            # SSE 는 사람이 읽는 안내를 stderr 로 (stdout 오염 방지)
            print(
                f"[context-loop] MCP 서버를 SSE 로 실행합니다 "
                f"(host={args.host}, port={args.port})",
                file=sys.stderr,
            )
            run_sse(port=args.port, host=args.host)
        else:
            # stdio 는 stdout 이 JSON-RPC 채널이므로 어떤 것도 stdout 으로 출력하지 않는다
            run_stdio()
        return 0

    parser.error("알 수 없는 명령입니다")
    return 2  # pragma: no cover (parser.error 가 SystemExit 발생)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
