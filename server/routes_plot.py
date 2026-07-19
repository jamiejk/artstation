"""Plot upload route registration."""

from __future__ import annotations

from fastapi import APIRouter

try:
    from server import server as srv
except ImportError:
    import server as srv


router = APIRouter()

router.add_api_route("/plot/layers", srv.plot_layers, methods=["POST"])
