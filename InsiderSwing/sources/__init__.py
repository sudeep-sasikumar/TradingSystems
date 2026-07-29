"""
InsiderSwing data sources.

Two implementations of the same interface (``sources/base.InsiderDataSource``):

  edgar_source.EdgarSource  — SEC EDGAR: free, complete, and the only source
                              that carries the Rule 10b5-1 checkbox.  Default,
                              and the only one usable for a historical backfill.

  fmp_source.FmpSource      — Financial Modeling Prep REST API.  Convenient for
                              a low-latency daily pull on a paid plan; on lower
                              tiers the per-symbol history endpoints return 403
                              and it never exposes the 10b5-1 flag.

Both populate the identical schema (ins_filings / ins_transactions), so nothing
downstream knows which source it is reading.

The factory lives in ``ingest.make_source`` — this package's modules use flat
absolute imports (matching the repo-wide convention for directories whose names
can't be Python packages), so keeping the factory beside its only caller avoids
a circular import through this file.
"""
