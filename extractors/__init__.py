"""
extractors package
==================
Business logic for extracting and paginating Kommo CRM entities.

Each extractor module owns:
  - Pydantic models for the entity (type-safe, validated)
  - Full pagination loop (page 1 → last page)
  - Dead-letter handling for records that fail validation
  - Structured logging per page and on completion

Modules:
  - leads     : Lead extraction + LeadModel
  - pipelines : Pipeline + Stage extraction + PipelineModel / StageModel
  - tasks     : Task extraction + TaskModel
"""
