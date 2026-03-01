"""CLI 진입점 (click 기반)."""

import click

from context_sync import __version__


@click.group()
@click.version_option(version=__version__, prog_name="context-sync")
def main() -> None:
    """Context Sync: 사내 문서를 로컬 LLM context로 동기화합니다."""


@main.command()
def init() -> None:
    """초기 설정 마법사를 실행합니다."""
    click.echo("초기 설정을 시작합니다...")


@main.command()
def start() -> None:
    """백그라운드 동기화 + 웹 UI를 시작합니다."""
    click.echo("동기화 서비스를 시작합니다...")


@main.command()
def stop() -> None:
    """백그라운드 동기화를 중지합니다."""
    click.echo("동기화 서비스를 중지합니다...")


@main.command()
def status() -> None:
    """동기화 상태를 확인합니다."""
    click.echo("동기화 상태를 확인합니다...")


@main.command()
@click.argument("question")
def ask(question: str) -> None:
    """CLI에서 직접 질문합니다."""
    click.echo(f"질문: {question}")
    click.echo("RAG 파이프라인이 아직 구현되지 않았습니다.")


@main.command(name="sync")
def sync_now() -> None:
    """수동 즉시 동기화를 실행합니다."""
    click.echo("수동 동기화를 실행합니다...")


@main.command()
def config() -> None:
    """설정 웹 UI를 엽니다."""
    click.echo("설정 UI를 엽니다...")


@main.command()
def autostart() -> None:
    """시스템 시작 시 자동 실행을 등록/해제합니다."""
    click.echo("자동 시작 설정을 변경합니다...")
