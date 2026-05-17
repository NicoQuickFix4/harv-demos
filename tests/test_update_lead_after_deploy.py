#!/usr/bin/env python3
"""Integratietest voor `update_lead_after_deploy()`.

Setup: maakt een tijdelijke SQLite-db met één testlead, mockt de Notion
PATCH-call, draait `update_lead_after_deploy()` en assert dat
  * de lead in SQLite naar fase 'demo_verstuurd' gaat
  * de Notion API met de juiste URL + payload aangeroepen wordt

Gebruik:
    python3 tests/test_update_lead_after_deploy.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Zorg dat we de demo-generator én de harv-scraper db kunnen importeren.
THIS_DIR = Path(__file__).resolve().parent
DEMOS_DIR = THIS_DIR.parent
SCRAPER_DIR = Path.home() / "Developer" / "harv-scraper"
sys.path.insert(0, str(DEMOS_DIR))
sys.path.insert(0, str(SCRAPER_DIR))

import db  # noqa: E402 — van harv-scraper
from generate_demo import update_lead_after_deploy  # noqa: E402


TEST_NOTION_PAGE_ID = "abcd1234-5678-90ab-cdef-1234567890ab"
TEST_WEBSITE = "https://testlead-makelaar.example"
TEST_DEMO_URL = "https://harv-demos.vercel.app/demo/testlead-makelaar/"


class UpdateLeadAfterDeployIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        # Tijdelijke DB-file zodat we de echte leads.db niet raken.
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "leads-test.db"
        db.init_db(self.db_path)
        db.upsert_lead(
            {
                "website": TEST_WEBSITE,
                "bedrijfsnaam": "Testlead Makelaar B.V.",
                "sector": "makelaardij",
                "stad": "Utrecht",
                "fase": "synced_to_notion",
            },
            db_path=self.db_path,
        )
        db.set_notion_page_id(TEST_WEBSITE, TEST_NOTION_PAGE_ID, db_path=self.db_path)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _fake_notion_response(self, status_code: int = 200) -> mock.MagicMock:
        resp = mock.MagicMock()
        resp.status_code = status_code
        resp.text = "{}"
        return resp

    def test_succesvolle_deploy_updates_fase_en_demolink(self) -> None:
        # Patch requests.patch zodat we niet écht naar Notion gaan
        with mock.patch("generate_demo.requests.patch", return_value=self._fake_notion_response()) as patched:
            status = update_lead_after_deploy(
                TEST_NOTION_PAGE_ID,
                TEST_DEMO_URL,
                db_path=self.db_path,
                notion_token="fake-token-for-test",
            )

        # 1. SQLite-fase moet bijgewerkt zijn
        lead = db.get_lead_by_id(TEST_WEBSITE, db_path=self.db_path)
        self.assertIsNotNone(lead, "testlead niet meer terug te vinden")
        self.assertEqual(lead["fase"], "demo_verstuurd")

        # 2. Notion PATCH moet ééns aangeroepen zijn met de juiste URL + payload
        self.assertEqual(patched.call_count, 1, "verwacht 1 PATCH-call naar Notion")
        call = patched.call_args
        # URL bevat de pagina-id
        self.assertIn(TEST_NOTION_PAGE_ID, call.args[0])
        # JSON-payload bevat Demo-link
        self.assertEqual(call.kwargs["json"]["properties"]["Demo-link"]["url"], TEST_DEMO_URL)
        # Header heeft het token
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer fake-token-for-test")

        # 3. Status-dict
        self.assertTrue(status["lead_found"], "lead had gevonden moeten worden")
        self.assertTrue(status["sqlite_updated"], "sqlite_updated had True moeten zijn")
        self.assertTrue(status["notion_updated"], "notion_updated had True moeten zijn")
        self.assertEqual(status["lead_id"], TEST_WEBSITE)
        self.assertEqual(status["errors"], [])

    def test_notion_faal_blokkeert_sqlite_update_niet(self) -> None:
        """Bij een 500 vanuit Notion moet SQLite alsnog naar demo_verstuurd."""
        with mock.patch(
            "generate_demo.requests.patch",
            return_value=self._fake_notion_response(status_code=500),
        ):
            status = update_lead_after_deploy(
                TEST_NOTION_PAGE_ID,
                TEST_DEMO_URL,
                db_path=self.db_path,
                notion_token="fake-token-for-test",
            )

        lead = db.get_lead_by_id(TEST_WEBSITE, db_path=self.db_path)
        self.assertEqual(lead["fase"], "demo_verstuurd", "SQLite moet onafhankelijk van Notion bijgewerkt zijn")
        self.assertTrue(status["sqlite_updated"])
        self.assertFalse(status["notion_updated"])
        self.assertTrue(any("HTTP 500" in e for e in status["errors"]))

    def test_lead_niet_gevonden_skipt_sqlite_maar_probeert_notion(self) -> None:
        """Onbekende notion_page_id → lead_found=False, maar Notion mag wel."""
        with mock.patch("generate_demo.requests.patch", return_value=self._fake_notion_response()) as patched:
            status = update_lead_after_deploy(
                "ffff0000-aaaa-bbbb-cccc-dddddddddddd",
                TEST_DEMO_URL,
                db_path=self.db_path,
                notion_token="fake-token-for-test",
            )

        self.assertFalse(status["lead_found"])
        self.assertFalse(status["sqlite_updated"])
        self.assertTrue(status["notion_updated"])
        self.assertEqual(patched.call_count, 1)

    def test_zonder_notion_token_skipt_notion_stap(self) -> None:
        with mock.patch("generate_demo.requests.patch") as patched:
            status = update_lead_after_deploy(
                TEST_NOTION_PAGE_ID,
                TEST_DEMO_URL,
                db_path=self.db_path,
                notion_token="",
            )
        # Zonder token mag requests.patch nooit aangeroepen worden
        self.assertEqual(patched.call_count, 0)
        # SQLite moet wél bijgewerkt zijn
        self.assertTrue(status["sqlite_updated"])
        self.assertFalse(status["notion_updated"])
        self.assertTrue(any("NOTION_TOKEN" in e for e in status["errors"]))


if __name__ == "__main__":
    # Zorg dat omgevings-NOTION_TOKEN het token-fixture-argument niet overschrijft.
    os.environ.pop("NOTION_TOKEN", None)
    os.environ.pop("NOTION_API_KEY", None)
    unittest.main(verbosity=2)
