"""vpa — Australian used-vehicle price analysis (local, personal-use).

Data flows one way:
    adapters (CSV/JSON/HTML/text/manual/v2-db)  ->  normalised Listing records
    -> store (SQLite, append + dedupe, price history kept)
    -> analyze (segment stats)  ->  forecast (ladder with intervals)
    -> report (tables, charts, CSV exports)
"""
__version__ = "1.0.0"
