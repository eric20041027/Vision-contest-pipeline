"""Role-scoped dataset access and the receipts it leaves behind (spec 2026-09-12). Nothing is
imported here on purpose: ``schema`` is reached from the measurement layer's schema, and
``access`` reaches the artifact layer, which reaches the measurement layer -- an import in this
file would close that loop."""
