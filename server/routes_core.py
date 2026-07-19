"""Core and control-page route registration."""

from __future__ import annotations

from fastapi import APIRouter

try:
    from server import server as srv
except ImportError:
    import server as srv


router = APIRouter()

router.add_api_route("/health", srv.health, methods=["GET"])
router.add_api_route("/control", srv.control_page, methods=["GET"])
router.add_api_route("/control/config", srv.control_config, methods=["GET"])
