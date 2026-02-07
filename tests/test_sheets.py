"""
Tests for Google Sheets API service.
"""
import pytest
from unittest.mock import MagicMock, patch

# Mark all tests as unit tests
pytestmark = pytest.mark.unit


class TestSheetsService:
    """Test SheetsService class."""

    def test_get_values_calls_api(self):
        """Should call Sheets API to get values."""
        from api.services.sheets import SheetsService

        mock_service = MagicMock()
        mock_service.spreadsheets().values().get().execute.return_value = {
            "values": [
                ["Header1", "Header2"],
                ["Value1", "Value2"],
            ]
        }

        with patch.object(SheetsService, "service", mock_service):
            service = SheetsService.__new__(SheetsService)
            service._service = mock_service
            service.account_type = MagicMock()

            result = service.get_values("sheet123", "Sheet1")

            assert result == [["Header1", "Header2"], ["Value1", "Value2"]]

    def test_get_sheet_with_headers_returns_dicts(self):
        """Should return list of dicts with headers as keys."""
        from api.services.sheets import SheetsService

        mock_service = MagicMock()
        mock_service.spreadsheets().values().get().execute.return_value = {
            "values": [
                ["Name", "Age", "City"],
                ["Alice", "30", "NYC"],
                ["Bob", "25", "LA"],
            ]
        }

        with patch.object(SheetsService, "service", mock_service):
            service = SheetsService.__new__(SheetsService)
            service._service = mock_service
            service.account_type = MagicMock()

            result = service.get_sheet_with_headers("sheet123", "Sheet1")

            assert len(result) == 2
            assert result[0] == {"Name": "Alice", "Age": "30", "City": "NYC"}
            assert result[1] == {"Name": "Bob", "Age": "25", "City": "LA"}

    def test_get_sheet_with_headers_pads_short_rows(self):
        """Should pad short rows with empty strings."""
        from api.services.sheets import SheetsService

        mock_service = MagicMock()
        mock_service.spreadsheets().values().get().execute.return_value = {
            "values": [
                ["Name", "Age", "City"],
                ["Alice"],  # Short row
            ]
        }

        with patch.object(SheetsService, "service", mock_service):
            service = SheetsService.__new__(SheetsService)
            service._service = mock_service
            service.account_type = MagicMock()

            result = service.get_sheet_with_headers("sheet123", "Sheet1")

            assert len(result) == 1
            assert result[0] == {"Name": "Alice", "Age": "", "City": ""}

    def test_get_sheet_with_headers_empty_sheet(self):
        """Should return empty list for sheet with only headers."""
        from api.services.sheets import SheetsService

        mock_service = MagicMock()
        mock_service.spreadsheets().values().get().execute.return_value = {
            "values": [["Header1", "Header2"]]
        }

        with patch.object(SheetsService, "service", mock_service):
            service = SheetsService.__new__(SheetsService)
            service._service = mock_service
            service.account_type = MagicMock()

            result = service.get_sheet_with_headers("sheet123", "Sheet1")

            assert result == []

    def test_get_spreadsheet_info(self):
        """Should return spreadsheet title and sheet names."""
        from api.services.sheets import SheetsService

        mock_service = MagicMock()
        mock_service.spreadsheets().get().execute.return_value = {
            "properties": {"title": "My Spreadsheet"},
            "sheets": [
                {"properties": {"title": "Sheet1"}},
                {"properties": {"title": "Form Responses 1"}},
            ]
        }

        with patch.object(SheetsService, "service", mock_service):
            service = SheetsService.__new__(SheetsService)
            service._service = mock_service
            service.account_type = MagicMock()

            result = service.get_spreadsheet_info("sheet123")

            assert result["title"] == "My Spreadsheet"
            assert result["sheets"] == ["Sheet1", "Form Responses 1"]


class TestGetSheetsService:
    """Test get_sheets_service factory function."""

    def test_returns_service_for_personal_account(self):
        """Should return SheetsService for personal account."""
        from api.services.sheets import get_sheets_service, _sheets_services
        from api.services.google_auth import GoogleAccount

        # Clear singleton
        _sheets_services.clear()

        with patch("api.services.sheets.get_google_auth") as mock_auth:
            mock_auth.return_value.get_credentials.return_value = MagicMock()
            with patch("api.services.sheets.build"):
                service = get_sheets_service(GoogleAccount.PERSONAL)

                assert service.account_type == GoogleAccount.PERSONAL

    def test_returns_same_instance_for_same_account(self):
        """Should return cached instance for same account type."""
        from api.services.sheets import get_sheets_service, _sheets_services
        from api.services.google_auth import GoogleAccount

        # Clear singleton
        _sheets_services.clear()

        with patch("api.services.sheets.get_google_auth") as mock_auth:
            mock_auth.return_value.get_credentials.return_value = MagicMock()
            with patch("api.services.sheets.build"):
                service1 = get_sheets_service(GoogleAccount.PERSONAL)
                service2 = get_sheets_service(GoogleAccount.PERSONAL)

                assert service1 is service2
