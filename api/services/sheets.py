"""
Google Sheets API service for LifeOS.

Provides read access to Google Sheets for syncing data to the vault.
"""
import logging
from typing import Optional

from googleapiclient.discovery import build

from api.services.google_auth import get_google_auth, GoogleAccount

logger = logging.getLogger(__name__)


class SheetsService:
    """
    Google Sheets service for reading spreadsheet data.

    Uses the Sheets API v4 for structured access to cells and ranges.
    """

    def __init__(self, account_type: GoogleAccount = GoogleAccount.PERSONAL):
        """
        Initialize Sheets service.

        Args:
            account_type: Which Google account to use
        """
        self.account_type = account_type
        self._service = None

    @property
    def service(self):
        """Get or create Sheets API service."""
        if self._service is None:
            auth = get_google_auth(self.account_type)
            credentials = auth.get_credentials()
            self._service = build("sheets", "v4", credentials=credentials)
        return self._service

    def get_values(self, spreadsheet_id: str, range: str) -> list[list[str]]:
        """
        Read values from a sheet range.

        Args:
            spreadsheet_id: The Google Sheets file ID
            range: A1 notation range (e.g., 'Sheet1!A:Z' or 'Form Responses 1')

        Returns:
            List of rows, where each row is a list of cell values
        """
        try:
            result = self.service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range=range
            ).execute()
            return result.get("values", [])
        except Exception as e:
            logger.error(f"Failed to read sheet {spreadsheet_id}: {e}")
            raise

    def get_sheet_with_headers(
        self,
        spreadsheet_id: str,
        range: str
    ) -> list[dict[str, str]]:
        """
        Read sheet as list of dicts using header row as keys.

        The first row is treated as headers. Each subsequent row
        becomes a dict mapping header names to cell values.

        Args:
            spreadsheet_id: The Google Sheets file ID
            range: A1 notation range (e.g., 'Form Responses 1')

        Returns:
            List of dicts, one per data row
        """
        values = self.get_values(spreadsheet_id, range)

        if len(values) < 2:
            logger.info(f"Sheet has fewer than 2 rows, returning empty list")
            return []

        headers = values[0]
        rows = []

        for row in values[1:]:
            # Pad row with empty strings if shorter than headers
            padded_row = row + [""] * (len(headers) - len(row))
            row_dict = dict(zip(headers, padded_row))
            rows.append(row_dict)

        return rows

    def get_spreadsheet_info(self, spreadsheet_id: str) -> dict:
        """
        Get metadata about a spreadsheet.

        Args:
            spreadsheet_id: The Google Sheets file ID

        Returns:
            Dict with title and list of sheet names
        """
        try:
            result = self.service.spreadsheets().get(
                spreadsheetId=spreadsheet_id,
                fields="properties.title,sheets.properties.title"
            ).execute()

            return {
                "title": result.get("properties", {}).get("title", ""),
                "sheets": [
                    s.get("properties", {}).get("title", "")
                    for s in result.get("sheets", [])
                ]
            }
        except Exception as e:
            logger.error(f"Failed to get spreadsheet info {spreadsheet_id}: {e}")
            raise


# Singleton services per account
_sheets_services: dict[GoogleAccount, SheetsService] = {}


def get_sheets_service(
    account_type: GoogleAccount = GoogleAccount.PERSONAL
) -> SheetsService:
    """Get or create Sheets service for an account."""
    if account_type not in _sheets_services:
        _sheets_services[account_type] = SheetsService(account_type)
    return _sheets_services[account_type]
