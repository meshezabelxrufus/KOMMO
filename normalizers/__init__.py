"""
normalizers/
============
Data normalisation and AI-export generation layer for Milestone 2.

Transforms raw Kommo CRM extraction outputs into structured,
AI-ready JSON files optimised for Claude analysis.

Available normalizers:
  daily_json_export  — Groups messages by date + lead_id into daily bundles
"""

from normalizers.daily_json_export import DailyExportGenerator, generate_daily_export

__all__ = ["DailyExportGenerator", "generate_daily_export"]
