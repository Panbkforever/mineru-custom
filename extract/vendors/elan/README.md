# Elan profile

Use `--vendor ELAN` (case-insensitive) or API form field `vendor=ELAN`.
CLI/API extraction passes the original PDF path to an optional vendor source
hook. TI, ST and ABOV do not enable that hook and keep their existing behavior.
No MinerU rerun is required: `--skip-parse` can reuse existing parse results.

## Evidence and output rules

- Native PDF borders and text coordinates must identify all four pin-table
  headers before source recovery is accepted. The reader uses the existing
  pdfminer.six dependency; it does not run OCR or call a model.
- Only existing physical-pin candidate pages and their immediate continuation
  pages are examined. Matching source grids restore missing headers/rows and
  true line breaks. Unrecognized or image-only grids keep the MinerU tables.
- Chinese/English field aliases map to the standard pin fields. Tilde ranges,
  descending ranges and multiline lists preserve source order.
- Pin names and types expand together only when cardinalities agree. A common
  I/O type stays intact. Different functions of the same physical number remain
  different records; no physical-pin aggregation is performed.
- Explicit numbered pin-description subsections/model scopes create separate
  mapping slots. Continuations retain the preceding scope. Package names or
  expected counts in filenames are never used to create or repair slots.

## Source discrepancies found during diagnosis

- EM78P210N's second subsection explicitly describes 24 pins, not the test
  workbook's 20. Its first subsection also prints VSS as 8; this is retained as
  source text, not silently changed to another number.
- EM78P342N has four independent mapping tables (14/16/18/20). The first is
  shared by D14/SO14; other tables also share package variants. The workbook's
  extra 14-pin group needs an explicit grouping rule before duplication.
- EM78P330/331N contains source inconsistencies: a GPIO name range and its pin
  list disagree in cardinality, and the second table literally prints TCC=57.
  The latter is not an OCR concatenation to split into 5 and 7. Conflicting
  cardinalities emit a warning and retain the source row without guessed pairs.

Validation must distinguish record count from unique physical pin count.
Multiple valid functions/OTP programming names can share one number.
Unit tests and focused original-PDF page fragments use no model or batch run.
