"""M12 Python dependency compatibility and resolution engine (ADR-009 to ADR-012).

Trust split: the resolver container's output is untrusted data. Everything in this package that
classifies inputs, verifies reports, computes fingerprints or writes evidence runs in the trusted
application process (the supervisor).
"""
