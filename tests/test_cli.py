"""CLI 테스트."""

from click.testing import CliRunner

from context_sync.cli import main


class TestCLI:
    def test_version(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "0.1.0" in result.output

    def test_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "context-sync" in result.output.lower() or "Context Sync" in result.output

    def test_init_command(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["init"])
        assert result.exit_code == 0

    def test_status_command(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0

    def test_ask_command(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["ask", "테스트 질문"])
        assert result.exit_code == 0
        assert "테스트 질문" in result.output
