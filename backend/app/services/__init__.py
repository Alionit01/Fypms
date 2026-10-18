"""Internal helpers extracted from ``app.main`` so endpoints stay thin.

Each module holds a cohesive slice of the logic that used to sit between
the route handlers in ``main.py``. Nothing here is a FastAPI app concern;
routers import from here, and ``main.py`` re-exports the names the test
suite patches or calls directly.
"""
