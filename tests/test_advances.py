from fastapi.testclient import TestClient

from autumn_ledger.api.server import create_app
from autumn_ledger.application import Ledger
from autumn_ledger.demo import request_data, synthetic_event
from tests.support import LedgerCase


BUSINESS_KEYS = ("data_revision", "references", "periods", "comparison", "quality",
                 "lineage", "lineage_digest", "semantics", "integrity", "statistical_checks")


class AdvanceTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.edition_id = self.edition()["id"]

    def advance(self, key="advance", source=None, **kwargs):
        return self.ledger.advances.advance(source or self.edition_id, key, **kwargs)

    def business(self, edition_id):
        result = self.ledger.editions.get(edition_id)["result"]
        return {key: result[key] for key in BUSINESS_KEYS}

    def full_at(self, revision):
        result = self.ledger.analysis.run(request_data(revision=revision))
        return {key: result[key] for key in BUSINESS_KEYS}

    def assert_matches_full(self, advance):
        self.assertEqual(advance["result_edition"]["data_revision"], advance["to_revision"])
        self.assertEqual(self.business(advance["result_edition_id"]), self.full_at(advance["to_revision"]))
        source_request = self.ledger.editions.get(self.edition_id)["request"]
        advanced_request = self.ledger.editions.get(advance["result_edition_id"])["request"]
        self.assertEqual(advanced_request, source_request)
        self.assertNotIn("revision", advanced_request)

    def test_single_order_correction_advances_incrementally(self):
        self.ledger.intake.ingest("alpha", "fix", [synthetic_event("a1", sequence=2, refund=10000)])
        advance = self.advance("one-fix")
        self.assertEqual(advance["mode"], "incremental")
        self.assertIsNone(advance["fallback"])
        self.assertFalse(advance["replayed"])
        read = advance["report"]["read"]
        self.assertEqual(read["order_events_scanned"], 1)
        self.assertEqual(read["order_revisions"],
                         [{"source": "alpha", "order_id": "a1", "sequence": 2,
                           "event_id": "a1-2", "revision": advance["to_revision"]}])
        self.assertGreater(read["order_events_total"], read["order_events_scanned"])
        self.assert_matches_full(advance)
        result = self.ledger.editions.get(advance["result_edition_id"])
        self.assertEqual(result["state"], "draft")
        self.assertEqual(result["version"], 1)
        self.assertFalse(result["stale"])
        self.assertEqual(result["result"]["periods"]["current"]["cube"]["totals"]["net_cents"], 110000)
        source = self.ledger.editions.get(self.edition_id)
        self.assertTrue(source["stale"])
        self.assertEqual(source["result"]["periods"]["current"]["cube"]["totals"]["net_cents"], 120000)
        self.assertTrue(self.ledger.editions.verify(self.edition_id)["ok"])
        self.assertTrue(self.ledger.database.integrity()["ok"])

    def test_order_move_withdraws_old_attribution(self):
        moved = synthetic_event("a1", day="2026-11-10", origin="nj", destination="museum", sequence=2)
        self.ledger.intake.ingest("alpha", "move", [moved])
        advance = self.advance("move")
        self.assertEqual(advance["mode"], "incremental")
        self.assert_matches_full(advance)
        result = self.ledger.editions.get(advance["result_edition_id"])["result"]
        self.assertEqual(result["periods"]["current"]["cube"]["totals"]["orders"], 2)
        self.assertEqual(result["periods"]["baseline"]["cube"]["totals"]["orders"], 3)
        change = advance["report"]["orders"]["changes"][0]
        self.assertEqual(change["order_id"], "a1")
        self.assertEqual(change["before_group"],
                         {"origin": "sz", "destination": "park", "travel_day": "2026-11-16"})
        self.assertEqual(change["after_group"],
                         {"origin": "nj", "destination": "museum", "travel_day": "2026-11-10"})
        self.assertEqual(change["before_periods"], ["current"])
        self.assertEqual(change["after_periods"], ["baseline"])
        self.assertTrue(change["group_changed"])
        scopes = advance["report"]["scopes"]
        self.assertEqual([row["order_id"] for row in scopes["current"]["orders"]["withdrawn"]], ["a1"])
        self.assertEqual([row["order_id"] for row in scopes["baseline"]["orders"]["added"]], ["a1"])
        self.assertTrue(scopes["current"]["recomputed"])
        self.assertTrue(scopes["baseline"]["recomputed"])
        full = self.ledger.analysis.run(request_data(revision=advance["to_revision"]))
        for name in ("current", "baseline"):
            self.assertEqual(result["periods"][name]["cube"]["origin_ranking"],
                             full["periods"][name]["cube"]["origin_ranking"])
            self.assertEqual(result["periods"][name]["profiles"]["origin"],
                             full["periods"][name]["profiles"]["origin"])

    def test_new_order_and_cancellation_apply_once(self):
        self.ledger.intake.ingest("alpha", "more", [synthetic_event("c1", gross=8000),
                                                    synthetic_event("a1", sequence=2, state="cancelled", refund=40000)])
        advance = self.advance("more")
        self.assertEqual(advance["mode"], "incremental")
        self.assertEqual(advance["report"]["read"]["order_events_scanned"], 2)
        self.assert_matches_full(advance)
        totals = self.ledger.editions.get(advance["result_edition_id"])["result"]["periods"]["current"]["cube"]["totals"]
        self.assertEqual(totals["orders"], 3)
        self.assertEqual(totals["cancelled_orders"], 1)
        retry = self.advance("more")
        self.assertTrue(retry["replayed"])
        self.assertEqual(retry["result_edition_id"], advance["result_edition_id"])
        again = self.ledger.editions.get(advance["result_edition_id"])["result"]["periods"]["current"]["cube"]["totals"]
        self.assertEqual(again["orders"], 3)
        self.assertEqual(again["cancelled_orders"], 1)

    def test_coverage_only_change_recomputes_without_new_orders(self):
        self.ledger.coverage.put({"source": "alpha", "city": "sz", "day": "2026-11-17",
                                  "status": "partial", "note": "未齐"}, 1)
        advance = self.advance("coverage")
        self.assertEqual(advance["mode"], "incremental")
        self.assertEqual(advance["report"]["read"]["order_events_scanned"], 0)
        self.assertEqual(advance["report"]["read"]["coverage_cells_reread"], 1)
        scope = advance["report"]["scopes"]["current"]
        self.assertEqual(scope["coverage_cells"],
                         [{"source": "alpha", "city": "sz", "day": "2026-11-17"}])
        self.assertTrue(scope["coverage_changed"])
        self.assertTrue(scope["recomputed"])
        self.assert_matches_full(advance)
        result = self.ledger.editions.get(advance["result_edition_id"])["result"]
        self.assertFalse(result["quality"]["publishable"])
        codes = {row["code"] for row in result["quality"]["issues"]}
        self.assertIn("partial_coverage", codes)

    def test_calendar_only_change_recomputes_exposure(self):
        self.ledger.calendars.put("sz-autumn", {"city": "sz", "start": "2026-11-17",
                                                "end": "2026-11-20", "label": "修订"}, 1)
        advance = self.advance("calendar")
        self.assertEqual(advance["mode"], "incremental")
        self.assertEqual(advance["report"]["read"]["order_events_scanned"], 0)
        self.assertEqual(advance["report"]["read"]["calendar_cities_reread"], ["sz"])
        self.assertTrue(advance["report"]["scopes"]["current"]["exposure_changed"])
        self.assert_matches_full(advance)

    def test_catalog_only_change_recomputes_references(self):
        self.ledger.catalog.put("city", "sz", {"name": "修订名称", "region": "合成区域"}, 1)
        advance = self.advance("catalog")
        self.assertEqual(advance["mode"], "incremental")
        changed = advance["report"]["read"]["reference_entries_changed"]
        self.assertEqual([(row["kind"], row["id"]) for row in changed], [("city", "sz")])
        self.assertIn("name", changed[0]["changed_fields"])
        self.assert_matches_full(advance)

    def test_unrelated_change_advances_without_recompute(self):
        self.ledger.intake.ingest("beta", "unrelated", [synthetic_event("x1", source="beta")])
        advance = self.advance("noop")
        self.assertEqual(advance["mode"], "incremental")
        self.assertEqual(advance["report"]["read"]["order_events_scanned"], 0)
        self.assertEqual([scope["recomputed"] for scope in advance["report"]["scopes"].values()],
                         [False, False])
        self.assertEqual(advance["report"]["derived"],
                         {"comparison": "reused", "quality": "reused", "lineage": "reused"})
        source = self.ledger.editions.get(self.edition_id)["result"]
        result = self.ledger.editions.get(advance["result_edition_id"])["result"]
        self.assertEqual(result["periods"], source["periods"])
        self.assertEqual(result["lineage_digest"], source["lineage_digest"])
        self.assertEqual(result["data_revision"], source["data_revision"] + 1)
        self.assert_matches_full(advance)

    def test_retry_same_advance_is_idempotent_and_conflict_on_different_request(self):
        self.ledger.intake.ingest("alpha", "fix", [synthetic_event("a1", sequence=2, refund=10000)])
        advance = self.advance("same")
        replay = self.advance("same")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["id"], advance["id"])
        self.assertEqual(replay["result_edition_id"], advance["result_edition_id"])
        self.assertEqual(len(self.ledger.advances.list()), 1)
        self.assertEqual(len(self.ledger.editions.list()), 2)
        self.assertError("conflict", self.advance, "same", target_revision=advance["to_revision"])
        self.assertError("conflict", self.advance, "same", delta_limit=10)
        self.ledger.intake.ingest("alpha", "later", [synthetic_event("a2", sequence=2, refund=1)])
        replayed_later = self.advance("same")
        self.assertTrue(replayed_later["replayed"])
        self.assertEqual(replayed_later["to_revision"], advance["to_revision"])
        self.assertEqual(len(self.ledger.editions.list()), 2)

    def test_restart_and_chained_advances_keep_progress(self):
        self.ledger.intake.ingest("alpha", "fix1", [synthetic_event("a1", sequence=2, refund=10000)])
        first = self.advance("chain-1")
        self.assert_matches_full(first)
        reopened = Ledger(self.path)
        replay = reopened.advances.advance(self.edition_id, "chain-1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["result_edition_id"], first["result_edition_id"])
        reopened.intake.ingest("alpha", "fix2", [synthetic_event("a2", sequence=2, refund=5000)])
        second = reopened.advances.advance(first["result_edition_id"], "chain-2")
        self.assertEqual(second["mode"], "incremental")
        self.assertEqual(second["from_revision"], first["to_revision"])
        result = reopened.editions.get(second["result_edition_id"])["result"]
        full = reopened.analysis.run(request_data(revision=second["to_revision"]))
        for key in BUSINESS_KEYS:
            self.assertEqual(result[key], full[key])
        self.assertEqual(result["periods"]["current"]["cube"]["totals"]["net_cents"], 105000)
        self.assertEqual(len(reopened.advances.list()), 2)

    def test_explicit_fallback_is_recorded_and_single_correction_stays_incremental(self):
        self.ledger.intake.ingest("alpha", "bulk", [synthetic_event("a1", sequence=2, refund=100),
                                                    synthetic_event("a2", sequence=2, refund=200)])
        advance = self.advance("bulk", delta_limit=1)
        self.assertEqual(advance["mode"], "full")
        self.assertEqual(advance["fallback"]["reason"], "delta_events_exceeded")
        self.assertEqual(advance["fallback"]["details"]["events"], 2)
        report = advance["report"]
        self.assertIsNone(report["read"]["order_revisions"])
        self.assertEqual(report["read"]["order_events_scanned"], report["read"]["order_events_total"])
        self.assertTrue(all(scope["recomputed"] for scope in report["scopes"].values()))
        self.assert_matches_full(advance)
        self.ledger.intake.ingest("alpha", "single", [synthetic_event("a3", sequence=2, refund=300)])
        tight = self.advance("single", source=advance["result_edition_id"], delta_limit=1)
        self.assertEqual(tight["mode"], "incremental")
        self.assertEqual(tight["report"]["read"]["order_events_scanned"], 1)
        self.assert_matches_full(tight)

    def test_multiple_revisions_of_same_order_use_latest_head(self):
        self.ledger.intake.ingest("alpha", "r1", [synthetic_event("a1", sequence=2, refund=10000)])
        self.ledger.intake.ingest("alpha", "r2", [synthetic_event("a1", sequence=3, refund=15000)])
        advance = self.advance("twice")
        self.assertEqual(advance["mode"], "incremental")
        self.assertEqual(advance["report"]["read"]["order_events_scanned"], 2)
        change = advance["report"]["orders"]["changes"][0]
        self.assertEqual(change["before_sequence"], 1)
        self.assertEqual(change["after_sequence"], 3)
        self.assert_matches_full(advance)
        totals = self.ledger.editions.get(advance["result_edition_id"])["result"]["periods"]["current"]["cube"]["totals"]
        self.assertEqual(totals["refund_cents"], 15000)

    def test_family_flag_flip_removes_order_from_family_analysis(self):
        flipped = synthetic_event("a1", sequence=2)
        flipped.update(family=False, children=0, adults=3)
        self.ledger.intake.ingest("alpha", "flip", [flipped])
        advance = self.advance("flip")
        self.assertEqual(advance["mode"], "incremental")
        self.assert_matches_full(advance)
        totals = self.ledger.editions.get(advance["result_edition_id"])["result"]["periods"]["current"]["cube"]["totals"]
        self.assertEqual(totals["orders"], 2)
        change = advance["report"]["orders"]["changes"][0]
        self.assertEqual(change["before_periods"], ["current"])
        self.assertEqual(change["after_periods"], [])

    def test_advance_validation_errors(self):
        self.assertError("invalid_target_revision", self.advance, "bad", target_revision=1)
        self.assertError("future_revision", self.advance, "bad", target_revision=10 ** 9)
        self.assertError("not_found", self.advance, "bad", source="ed-missing")
        self.assertError("invalid_id", self.advance, "bad key!")
        self.assertError("invalid_target_revision", self.advance, "bad",
                         target_revision=self.ledger.database.revision())
        pinned = self.ledger.editions.create("pinned", request_data(revision=self.ledger.database.revision()))
        self.ledger.intake.ingest("alpha", "fix", [synthetic_event("a1", sequence=2, refund=1)])
        self.assertError("pinned_revision", self.advance, "bad", source=pinned["id"])
        self.assertError("not_found", self.ledger.advances.get, "adv-missing")

    def test_advanced_edition_flows_through_review_and_compares(self):
        self.ledger.intake.ingest("alpha", "fix", [synthetic_event("a1", sequence=2, refund=10000)])
        advance = self.advance("review")
        result_id = advance["result_edition_id"]
        edition = self.ledger.editions.get(result_id)
        approved = self.ledger.review.decide(result_id, "approve", edition["version"], "analyst", "增量结果已核对")
        self.assertEqual(approved["state"], "approved")
        published = self.ledger.review.publish(result_id, "weekly", approved["version"], 0, "publisher")
        self.assertEqual(published["generation"], 1)
        comparison = self.ledger.editions.compare_editions(self.edition_id, result_id)
        self.assertTrue(comparison["same_request"])
        self.assertEqual(comparison["metrics"]["net_cents"]["change"], -10000)
        self.assertEqual(comparison["added_heads"], [["alpha", "a1", 2]])
        self.assertEqual(comparison["removed_heads"], [["alpha", "a1", 1]])

    def test_advance_report_supports_performance_and_consistency_audit(self):
        self.ledger.intake.ingest("alpha", "fix", [synthetic_event("a1", sequence=2, refund=10000)])
        advance = self.advance("audit")
        fetched = self.ledger.advances.get(advance["id"])
        self.assertEqual(fetched["advance_key"], "audit")
        report = fetched["report"]
        self.assertEqual(report["from_revision"], self.ledger.editions.get(self.edition_id)["data_revision"])
        self.assertEqual(report["to_revision"], advance["to_revision"])
        consistency = report["consistency"]
        self.assertTrue(consistency["integrity_ok"])
        self.assertTrue(consistency["statistical_checks_ok"])
        self.assertEqual(consistency["reconciliation"], {"current": True, "baseline": True})
        self.assertNotEqual(consistency["lineage_digest_before"], consistency["lineage_digest_after"])
        self.assertEqual(consistency["result_digest"],
                         self.ledger.editions.get(advance["result_edition_id"])["result_digest"])
        listed = self.ledger.advances.list(self.edition_id)
        self.assertEqual([row["id"] for row in listed], [advance["id"]])
        chained = self.ledger.advances.list(advance["result_edition_id"])
        self.assertEqual([row["id"] for row in chained], [advance["id"]])
        self.assertEqual(fetched["result_edition"]["id"], advance["result_edition_id"])


class AdvanceApiTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.app = create_app(self.path)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.edition_id = self.edition()["id"]

    def test_http_advance_lifecycle_and_audit_queries(self):
        correction = self.client.post("/sources/alpha/batches", json={
            "batch_id": "http-fix", "events": [synthetic_event("a1", sequence=2, refund=10000)]})
        self.assertEqual(correction.status_code, 200, correction.text)
        created = self.client.post(f"/editions/{self.edition_id}/advances", json={"advance_key": "http-adv"})
        self.assertEqual(created.status_code, 200, created.text)
        advance = created.json()
        self.assertEqual(advance["mode"], "incremental")
        self.assertFalse(advance["replayed"])
        self.assertEqual(advance["result_edition"]["summary"]["net_cents"], 110000)
        replay = self.client.post(f"/editions/{self.edition_id}/advances", json={"advance_key": "http-adv"})
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(replay.json()["result_edition_id"], advance["result_edition_id"])
        conflict = self.client.post(f"/editions/{self.edition_id}/advances",
                                    json={"advance_key": "http-adv", "delta_limit": 5})
        self.assertEqual(conflict.status_code, 409)
        fetched = self.client.get(f"/advances/{advance['id']}")
        self.assertEqual(fetched.status_code, 200)
        report = fetched.json()["report"]
        self.assertEqual(report["read"]["order_events_scanned"], 1)
        self.assertEqual(report["read"]["order_revisions"][0]["order_id"], "a1")
        self.assertTrue(report["consistency"]["integrity_ok"])
        listed = self.client.get("/advances").json()
        self.assertEqual([row["id"] for row in listed], [advance["id"]])
        by_edition = self.client.get(f"/editions/{self.edition_id}/advances").json()
        self.assertEqual([row["id"] for row in by_edition], [advance["id"]])
        missing = self.client.get("/advances/adv-missing")
        self.assertEqual(missing.status_code, 404)
        result_edition = self.client.get(f"/editions/{advance['result_edition_id']}")
        self.assertEqual(result_edition.status_code, 200)
        self.assertFalse(result_edition.json()["stale"])
        invalid = self.client.post(f"/editions/{self.edition_id}/advances",
                                   json={"advance_key": "bad-target", "target_revision": 10 ** 9})
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["error"]["code"], "future_revision")

    def test_http_advance_rejects_unknown_fields(self):
        response = self.client.post(f"/editions/{self.edition_id}/advances",
                                    json={"advance_key": "x", "mode": "full"})
        self.assertEqual(response.status_code, 422)
