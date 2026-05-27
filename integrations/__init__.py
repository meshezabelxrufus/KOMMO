"""
integrations/
=============
Third-party integration adapters for the Kommo CRM automation system.

Milestone 2 — Google Sheets integration layer.

Available integrations:
  google_sheets  — Batch write CRM data to Google Sheets via Service Account
"""

from integrations.google_sheets import GoogleSheetsClient

__all__ = ["GoogleSheetsClient"]
