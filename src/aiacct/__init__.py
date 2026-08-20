"""AI Accounting Assistant.

Reads a client's bank statement and supporting documents, codes each
transaction to a general ledger account, scores how sure it is, and routes
what it cannot resolve to an accountant. Corrections become durable memory so
the same client needs less review each month.
"""

__version__ = "0.1.0"
