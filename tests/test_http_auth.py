"""Auth rejection tests.

These tests verify that check_token rejects missing and wrong tokens,
and that protected routes enforce the token.  They complement the
existing test_server.py tests (which always pass the correct token)
by exercising the rejection paths.
"""
import unittest
from unittest import mock

from fastapi import HTTPException

# Reuse the server module that test_server.py already configured.
from tests.test_server import server


class CheckTokenTests(unittest.TestCase):
    def test_missing_token_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            server.check_token(None)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_wrong_token_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            server.check_token("wrong-token")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_empty_string_token_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            server.check_token("")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_correct_token_is_accepted(self):
        # Should not raise
        server.check_token("test-token")

    def test_uses_constant_time_comparison(self):
        """Verify secrets.compare_digest is used (not plain ==)."""
        import inspect
        source = inspect.getsource(server.check_token)
        self.assertIn("compare_digest", source)


class ProtectedRouteTests(unittest.TestCase):
    """Verify that protected routes actually call check_token."""

    def _mock_request(self):
        req = mock.Mock()
        req.client.host = "127.0.0.1"
        return req

    def test_health_rejects_missing_token(self):
        with self.assertRaises(HTTPException) as ctx:
            server.health(x_plotter_token=None)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_health_rejects_wrong_token(self):
        with self.assertRaises(HTTPException) as ctx:
            server.health(x_plotter_token="wrong")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_health_accepts_correct_token(self):
        result = server.health(x_plotter_token="test-token")
        self.assertTrue(result["ok"])
        self.assertTrue(result["token_required"])

    def test_list_jobs_rejects_missing_token(self):
        with self.assertRaises(HTTPException) as ctx:
            server.list_jobs(x_plotter_token=None)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_list_jobs_rejects_wrong_token(self):
        with self.assertRaises(HTTPException) as ctx:
            server.list_jobs(x_plotter_token="nope")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_plotter_state_rejects_missing_token(self):
        with self.assertRaises(HTTPException) as ctx:
            server.plotter_state(x_plotter_token=None)
        self.assertEqual(ctx.exception.status_code, 401)


class PublicRouteTests(unittest.TestCase):
    """Verify that public routes do NOT require a token."""

    def test_control_config_does_not_check_token(self):
        """control_config only requires localhost, not a token."""
        import inspect
        source = inspect.getsource(server.control_config)
        self.assertNotIn("check_token", source)

    def test_control_page_does_not_check_token(self):
        import inspect
        source = inspect.getsource(server.control_page)
        self.assertNotIn("check_token", source)


if __name__ == "__main__":
    unittest.main()
