"""Job route registration."""

from __future__ import annotations

from fastapi import APIRouter

try:
    from server import server as srv
except ImportError:
    import server as srv


router = APIRouter()

router.add_api_route("/jobs", srv.list_jobs, methods=["GET"])
router.add_api_route("/jobs/{job_id}", srv.get_job, methods=["GET"])
router.add_api_route("/jobs/{job_id}/layers/{layer_index}/preview.svg", srv.job_layer_preview, methods=["GET"])
router.add_api_route("/jobs/{job_id}/cancel", srv.cancel_job, methods=["POST"])
router.add_api_route("/jobs/{job_id}/log", srv.get_job_log, methods=["GET"])
router.add_api_route("/jobs/{job_id}/pause", srv.pause_job, methods=["POST"])
router.add_api_route("/jobs/{job_id}/resume", srv.resume_job, methods=["POST"])
router.add_api_route("/jobs/{job_id}/dip_recovery", srv.recover_dip_job, methods=["POST"])
router.add_api_route("/jobs/{job_id}/dip_now", srv.dip_paused_job_now, methods=["POST"])
router.add_api_route("/jobs/{job_id}/dip_interval", srv.update_job_dip_interval, methods=["POST"])
router.add_api_route("/jobs/{job_id}/auto_dip", srv.update_job_auto_dip, methods=["POST"])
router.add_api_route("/jobs/clear", srv.clear_jobs, methods=["POST"])
router.add_api_route("/jobs/{job_id}/delete", srv.delete_job, methods=["POST"])
router.add_api_route("/jobs/{job_id}/rerun", srv.rerun_job, methods=["POST"])
