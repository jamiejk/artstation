"""Operator console route registration."""

from __future__ import annotations

from fastapi import APIRouter

try:
    from server import server as srv
except ImportError:
    import server as srv


router = APIRouter()

router.add_api_route("/operator/next", srv.operator_next, methods=["GET"])
router.add_api_route("/operator/continue", srv.operator_continue, methods=["POST"])
