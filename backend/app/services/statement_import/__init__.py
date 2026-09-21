"""Statement ingestion: bank files in, Securo's own import DTO out.

The layer sits strictly *in front of* `import_service`. A provider adapter
turns a PDF, spreadsheet or CSV into normalized rows; the normalizer turns
those into `TransactionImport`; and from there the existing importer does
everything it already does for OFX, QIF, CAMT and CSV — duplicate detection,
categories, payees, rules, recurring matching, FX and the import log. No
business logic downstream of that boundary is re-implemented here.
"""
