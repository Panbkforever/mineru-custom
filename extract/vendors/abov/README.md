# ABOV extraction profile

Select `--vendor ABOV` in the CLI/batch script, or form field `vendor=ABOV`
in the existing API. TI remains the default; ST rules are not imported here.

## Supported source structures

- Shared pin tables with multiple physical package columns. Repeated package
  bases carrying distinct `(1)` / `(2)` pinout variants remain separate slots.
- Each source function row retains its own pin number, name, type and
  description. Repeated numbers do not cause ABOV to combine rows or join
  names/types. Shared exact-record deduplication remains unchanged.
- Explicit ascending/descending numeric ranges, `to`, and Unicode dashes.
  Parallel signal ranges expand only when their lengths agree with pin ranges.
- Wrapped numeric digits, only when both outer sequence anchors confirm a
  complete consecutive run. Explicit parallel signal lines prevent joining.
- Parenthesized alternative number mappings, only when the complete table
  proves two distinct, equal-coverage bijections. Both mappings are retained;
  generic labels distinguish them if package names are unavailable locally.

The display-driver table using primary and parenthesized package numbers
contains two mappings even when a test filename lists just one pin count.
Do not discard one mapping or synthesize pins to match filename expectations.

No rule depends on filenames, model names, expected counts or missing-pin lists.
Uncertain digit joins and range-to-name correspondences are not guessed.
Tests use small in-memory tables without MinerU, LLM or network calls.
