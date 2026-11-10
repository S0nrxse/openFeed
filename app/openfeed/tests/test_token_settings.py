import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from openfeed import main
from openfeed.tests.support import offline_db


class TokenSettingsTests(unittest.TestCase):
    def test_settings_has_token_controls_for_browser_users_only(self):
        user = {"authenticated": True, "display_name": "Alice", "email": "alice@example.test", "sub": "alice"}
        with offline_db():
            html = main._settings_dialog(user, "alice")
        self.assertIn('data-settings-pane="api-tokens"', html)
        self.assertIn('data-api-token-form', html)
        self.assertIn('value="read" checked', html)
        self.assertIn('/assets/api-tokens-v1.js', html)
        self.assertNotIn('ofd_', html)
        # Anonymous with sign-in on: no form. (Sign-in off is the local profile's: test_system_ui.)
        with patch.dict("os.environ", {"OPENFEED_AUTH_MODE": "authentik_oidc"}):
            self.assertNotIn('data-api-token-form', main._api_tokens_pane({"authenticated": False}))
        self.assertNotIn('data-api-token-form', main._api_tokens_pane({"authenticated": True, "via_token": True}))

    def test_old_settings_link_opens_actual_token_pane(self):
        with patch.object(main.db, "connect", side_effect=AssertionError("Unexpected DB")):
            client = TestClient(main.app)
            try:
                response = client.get('/settings', follow_redirects=False)
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.headers['location'], '/?settings=api-tokens')
            finally:
                client.close()

    def test_only_named_token_script_is_served(self):
        client = TestClient(main.app)
        try:
            response = client.get('/assets/api-tokens-v1.js')
            self.assertEqual(response.status_code, 200)
            self.assertIn('text/javascript', response.headers['content-type'])
            self.assertEqual(client.get('/assets/not-allowed.js').status_code, 404)
        finally:
            client.close()
