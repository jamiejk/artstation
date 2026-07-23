"""Plotter-control route registration."""

from __future__ import annotations

from fastapi import APIRouter

try:
    from server import server as srv
except ImportError:
    import server as srv


router = APIRouter()

router.add_api_route("/plotter/state", srv.plotter_state, methods=["GET"])
router.add_api_route("/plotter/pen", srv.plotter_pen, methods=["POST"])
router.add_api_route("/plotter/pen_settings", srv.plotter_pen_settings, methods=["POST"])
router.add_api_route("/plotter/pen/jog", srv.plotter_pen_jog, methods=["POST"])
router.add_api_route("/plotter/pen/seat", srv.plotter_pen_seat, methods=["POST"])
router.add_api_route("/plotter/pen/calibrate", srv.plotter_pen_calibrate, methods=["POST"])
router.add_api_route("/plotter/plot_settings", srv.plotter_plot_settings, methods=["POST"])
router.add_api_route("/plotter/paper", srv.plotter_paper, methods=["GET"])
router.add_api_route("/plotter/paper", srv.plotter_paper_update, methods=["POST"])
router.add_api_route("/plotter/ink_well", srv.plotter_ink_well, methods=["GET"])
router.add_api_route("/plotter/ink_well", srv.plotter_ink_well_update, methods=["POST"])
router.add_api_route("/plotter/ink_well/test", srv.plotter_ink_well_test, methods=["POST"])
router.add_api_route("/plotter/ink_well/confirm_test", srv.plotter_ink_well_confirm_test, methods=["POST"])
router.add_api_route("/plotter/motors", srv.plotter_motors, methods=["POST"])
router.add_api_route("/plotter/home/set", srv.plotter_set_home, methods=["POST"])
router.add_api_route("/plotter/position/set", srv.plotter_set_position, methods=["POST"])
router.add_api_route("/plotter/position/calibration", srv.plotter_position_calibration_toggle, methods=["POST"])
router.add_api_route("/plotter/home/return", srv.plotter_return_home, methods=["POST"])
router.add_api_route("/plotter/move", srv.plotter_move, methods=["POST"])
router.add_api_route("/plotter/jog", srv.plotter_jog, methods=["POST"])
router.add_api_route("/plotter/move_to", srv.plotter_move_to, methods=["POST"])
