"""auth 모듈 테스트.

keyring을 모킹하여 OS 키체인 없이 테스트한다.
"""

from unittest.mock import MagicMock, patch

from context_sync.auth import (
    SERVICE_NAME,
    delete_token,
    get_token,
    has_token,
    store_token,
)


@patch("context_sync.auth.keyring")
class TestStoreToken:
    def test_stores_token_in_keyring(self, mock_keyring: MagicMock) -> None:
        store_token("confluence", "api_token", "my-secret-token")
        mock_keyring.set_password.assert_called_once_with(
            SERVICE_NAME, "confluence:api_token", "my-secret-token"
        )

    def test_stores_openai_key(self, mock_keyring: MagicMock) -> None:
        store_token("openai", "api_key", "sk-xxx")
        mock_keyring.set_password.assert_called_once_with(
            SERVICE_NAME, "openai:api_key", "sk-xxx"
        )


@patch("context_sync.auth.keyring")
class TestGetToken:
    def test_returns_token_when_exists(self, mock_keyring: MagicMock) -> None:
        mock_keyring.get_password.return_value = "my-secret-token"
        result = get_token("confluence", "api_token")
        assert result == "my-secret-token"
        mock_keyring.get_password.assert_called_once_with(
            SERVICE_NAME, "confluence:api_token"
        )

    def test_returns_none_when_not_exists(self, mock_keyring: MagicMock) -> None:
        mock_keyring.get_password.return_value = None
        result = get_token("confluence", "api_token")
        assert result is None


@patch("context_sync.auth.keyring")
class TestDeleteToken:
    def test_returns_true_on_success(self, mock_keyring: MagicMock) -> None:
        result = delete_token("confluence", "api_token")
        assert result is True
        mock_keyring.delete_password.assert_called_once_with(
            SERVICE_NAME, "confluence:api_token"
        )

    def test_returns_false_on_not_found(self, mock_keyring: MagicMock) -> None:
        from keyring.errors import PasswordDeleteError

        mock_keyring.delete_password.side_effect = PasswordDeleteError()
        result = delete_token("confluence", "api_token")
        assert result is False


@patch("context_sync.auth.keyring")
class TestHasToken:
    def test_returns_true_when_exists(self, mock_keyring: MagicMock) -> None:
        mock_keyring.get_password.return_value = "token"
        assert has_token("confluence", "api_token") is True

    def test_returns_false_when_not_exists(self, mock_keyring: MagicMock) -> None:
        mock_keyring.get_password.return_value = None
        assert has_token("confluence", "api_token") is False
