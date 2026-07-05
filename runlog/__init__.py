"""runlog: extract and locally store running fitness data.

Sources: the Strava API, Strava bulk-export archives, and Apple Health
exports. Data lands verbatim in a raw archive and is normalized into a local
SQLite database for querying and future analysis.
"""

__version__ = "0.1.0"
