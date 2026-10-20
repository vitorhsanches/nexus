"""Nexus D.4.4 qualification boundary tests; no network is permitted."""
import copy
import os
import unittest
import urllib.error

from nexus.routing.catalog import APPROVED_CATALOG
from nexus.routing.qualification_framework import QualificationCandidate, QualificationVerdict, QualificationEvidenceType
from nexus.routing.qualification_benchmark import QualificationClient, PROBE, SENTINEL


class FakeTransport:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls: list[tuple[str, dict, dict, float]] = []

    def post_json(self, url, headers, payload, timeout):
        self.calls.append((url, headers, payload, timeout))
        if self.error:
            raise self.error
        return self.response


def cand(model="openai/gpt-5", provider="openai", execution_path="OMNIROUTE", effort="high", capability="standard-coding", risk="medium"):
    return QualificationCandidate(model, provider, execution_path, effort, capability, risk)


def run(client, c=None, **kw):
    return client.qualify(c or cand(), fleet_modality="text", route_kind="fixed", normalization_status="resolved", **kw)


class D44PayloadTests(unittest.TestCase):
    def test_payload_contains_model_and_effort(self):
        c = cand(model="openai/gpt-5", effort="high")
        t = FakeTransport({"model": c.model_id, "output_text": SENTINEL})
        r = run(QualificationClient(transport=t), c)
        self.assertEqual(r.verdict, QualificationVerdict.PASS)
        self.assertEqual(len(t.calls), 1)
        _, _, payload, _ = t.calls[0]
        self.assertEqual(payload["model"], c.model_id)
        self.assertIn("reasoning", payload)
        self.assertEqual(payload["reasoning"]["effort"], c.effort)
        self.assertEqual(payload["input"], PROBE)

    def test_payload_effort_varies_with_candidate(self):
        for effort in ("high", "low", "medium"):
            c = cand(effort=effort)
            t = FakeTransport({"model": c.model_id, "output_text": SENTINEL})
            run(QualificationClient(transport=t), c)
            self.assertEqual(t.calls[0][2]["reasoning"]["effort"], effort)


class D44EvidenceBindingTests(unittest.TestCase):
    def test_all_evidence_binds_on_pass(self):
        c = cand()
        t = FakeTransport({"model": c.model_id, "output_text": SENTINEL})
        r = run(QualificationClient(transport=t), c)
        self.assertEqual(r.verdict, QualificationVerdict.PASS)
        for ev in r.evidence:
            self.assertTrue(ev.binds_to(c), f"{ev.evidence_type} failed binds_to")
            self.assertEqual(ev.model_id, c.model_id)
            self.assertEqual(ev.provider, c.provider)
            self.assertEqual(ev.execution_path, c.execution_path)
            self.assertEqual(ev.effort, c.effort)
            self.assertEqual(ev.capability, c.capability)
            self.assertEqual(ev.risk, c.risk)

    def test_all_evidence_binds_on_fail_host(self):
        c = cand()
        t = FakeTransport(error=TimeoutError("timeout"))
        r = run(QualificationClient(transport=t), c)
        self.assertEqual(r.verdict, QualificationVerdict.FAIL)
        for ev in r.evidence:
            self.assertTrue(ev.binds_to(c))
            self.assertEqual(ev.model_id, c.model_id)
            self.assertEqual(ev.provider, c.provider)
            self.assertEqual(ev.execution_path, c.execution_path)
            self.assertEqual(ev.effort, c.effort)

    def test_evidence_binding_on_identity_mismatch(self):
        c = cand(model="openai/gpt-5")
        t = FakeTransport({"model": "other/model", "output_text": SENTINEL})
        r = run(QualificationClient(transport=t), c)
        self.assertEqual(r.verdict, QualificationVerdict.FAIL)
        for ev in r.evidence:
            self.assertTrue(ev.binds_to(c))
            self.assertEqual(ev.model_id, c.model_id)
        # returned model only in detail/meta not in model_id field
        identity_ev = [e for e in r.evidence if e.evidence_type == QualificationEvidenceType.EXACT_IDENTITY_CHECK][0]
        self.assertEqual(identity_ev.model_id, c.model_id)
        self.assertIn("other/model", identity_ev.detail)
        self.assertEqual(identity_ev.meta.get("returned_model"), "other/model")

    def test_mandatory_evidence_types_present(self):
        c = cand()
        t = FakeTransport({"model": c.model_id, "output_text": SENTINEL})
        r = run(QualificationClient(transport=t), c)
        types = {e.evidence_type for e in r.evidence}
        self.assertIn(QualificationEvidenceType.EXACT_IDENTITY_CHECK, types)
        self.assertIn(QualificationEvidenceType.HOST_VALIDATION, types)
        self.assertIn(QualificationEvidenceType.SEMANTIC_REVIEW, types)


class D44ResponseParsingTests(unittest.TestCase):
    def test_top_level_output_text_success(self):
        c = cand()
        t = FakeTransport({"model": c.model_id, "output_text": SENTINEL})
        r = run(QualificationClient(transport=t), c)
        self.assertEqual(r.verdict, QualificationVerdict.PASS)

    def test_nested_output_content_text_success(self):
        c = cand()
        t = FakeTransport({"model": c.model_id, "output": [{"content": [{"text": SENTINEL}]}]})
        r = run(QualificationClient(transport=t), c)
        self.assertEqual(r.verdict, QualificationVerdict.PASS)

    def test_nested_output_text_key(self):
        c = cand()
        t = FakeTransport({"model": c.model_id, "output": [{"content": [{"output_text": SENTINEL}]}]})
        r = run(QualificationClient(transport=t), c)
        self.assertEqual(r.verdict, QualificationVerdict.PASS)

    def test_nested_output_item_text(self):
        c = cand()
        t = FakeTransport({"model": c.model_id, "output": [{"text": SENTINEL}]})
        r = run(QualificationClient(transport=t), c)
        self.assertEqual(r.verdict, QualificationVerdict.PASS)

    def test_model_mismatch_fails(self):
        c = cand()
        t = FakeTransport({"model": "different/model", "output_text": SENTINEL})
        r = run(QualificationClient(transport=t), c)
        self.assertEqual(r.verdict, QualificationVerdict.FAIL)

    def test_missing_model_fails(self):
        c = cand()
        t = FakeTransport({"output_text": SENTINEL})
        r = run(QualificationClient(transport=t), c)
        self.assertEqual(r.verdict, QualificationVerdict.FAIL)

    def test_semantic_miss_fails(self):
        c = cand()
        t = FakeTransport({"model": c.model_id, "output_text": "no sentinel here"})
        r = run(QualificationClient(transport=t), c)
        self.assertEqual(r.verdict, QualificationVerdict.FAIL)

    def test_malformed_response_not_dict(self):
        c = cand()
        t = FakeTransport("not a dict")
        r = run(QualificationClient(transport=t), c)
        self.assertEqual(r.verdict, QualificationVerdict.FAIL)
        self.assertTrue(any(not e.passed for e in r.evidence))

    def test_malformed_response_none(self):
        c = cand()
        t = FakeTransport(None)
        r = run(QualificationClient(transport=t), c)
        self.assertEqual(r.verdict, QualificationVerdict.FAIL)


class D44FailureTests(unittest.TestCase):
    def test_timeout_returns_fail_bound_to_candidate(self):
        c = cand()
        t = FakeTransport(error=TimeoutError("timeout"))
        r = run(QualificationClient(transport=t), c)
        self.assertEqual(r.verdict, QualificationVerdict.FAIL)
        self.assertTrue(r.evidence[0].binds_to(c))
        self.assertIn("timeout", r.evidence[0].detail.lower())

    def test_os_error_returns_fail(self):
        c = cand()
        t = FakeTransport(error=OSError("wire"))
        r = run(QualificationClient(transport=t), c)
        self.assertEqual(r.verdict, QualificationVerdict.FAIL)

    def test_http_error_returns_fail(self):
        c = cand()
        err = urllib.error.HTTPError("http://x", 500, "Internal", {}, None)
        t = FakeTransport(error=err)
        r = run(QualificationClient(transport=t), c)
        self.assertEqual(r.verdict, QualificationVerdict.FAIL)
        self.assertTrue(r.evidence[0].binds_to(c))

    def test_generic_exception_secret_redaction(self):
        c = cand()
        t = FakeTransport(error=RuntimeError("Bearer sk-secret123"))
        r = run(QualificationClient(api_key="TOPSECRET", transport=t), c)
        self.assertEqual(r.verdict, QualificationVerdict.FAIL)
        combined = " ".join(e.detail for e in r.evidence) + repr(r)
        self.assertNotIn("TOPSECRET", combined)
        self.assertNotIn("sk-secret123", combined)
        self.assertNotIn("Bearer", combined)

    def test_qualification_error_secret_redaction(self):
        from nexus.routing.qualification_benchmark import QualificationError
        c = cand()
        t = FakeTransport(error=QualificationError("Bearer sk-abc top secret"))
        r = run(QualificationClient(api_key="mykey123", transport=t), c)
        self.assertEqual(r.verdict, QualificationVerdict.FAIL)
        for ev in r.evidence:
            self.assertNotIn("sk-abc", ev.detail)
            self.assertNotIn("mykey123", ev.detail)

    def test_http_failure_not_expose_api_key(self):
        c = cand()
        t = FakeTransport(error=OSError("fail"))
        r = run(QualificationClient(api_key="SECRET_KEY_123", transport=t), c)
        self.assertEqual(r.verdict, QualificationVerdict.FAIL)
        self.assertNotIn("SECRET_KEY_123", repr(r))
        for ev in r.evidence:
            self.assertNotIn("SECRET_KEY_123", ev.detail)


class D44NoNetworkGateTests(unittest.TestCase):
    def test_terra_blocked_no_network(self):
        c = cand(model="gpt-5.6-terra")
        t = FakeTransport({"model": c.model_id, "output_text": SENTINEL})
        r = run(QualificationClient(transport=t), c)
        self.assertEqual(r.verdict, QualificationVerdict.REJECT)
        self.assertEqual(len(t.calls), 0)

    def test_unresolved_normalization_no_network(self):
        c = cand()
        t = FakeTransport({"model": c.model_id, "output_text": SENTINEL})
        r = QualificationClient(transport=t).qualify(c, fleet_modality="text", route_kind="fixed", normalization_status="unresolved")
        self.assertEqual(r.verdict, QualificationVerdict.REJECT)
        self.assertEqual(len(t.calls), 0)

    def test_unresolved_normalization_none_no_network(self):
        c = cand()
        t = FakeTransport({"model": c.model_id, "output_text": SENTINEL})
        r = QualificationClient(transport=t).qualify(c, fleet_modality="text", route_kind="fixed", normalization_status=None)
        self.assertEqual(r.verdict, QualificationVerdict.REJECT)
        self.assertEqual(len(t.calls), 0)

    def test_unknown_route_no_network(self):
        c = cand()
        t = FakeTransport({"model": c.model_id, "output_text": SENTINEL})
        r = QualificationClient(transport=t).qualify(c, fleet_modality="text", route_kind="unknown", normalization_status="resolved")
        self.assertEqual(r.verdict, QualificationVerdict.REJECT)
        self.assertEqual(len(t.calls), 0)

    def test_pool_route_no_network(self):
        c = cand()
        t = FakeTransport({"model": c.model_id, "output_text": SENTINEL})
        r = QualificationClient(transport=t).qualify(c, fleet_modality="text", route_kind="pool", normalization_status="resolved")
        self.assertEqual(r.verdict, QualificationVerdict.REJECT)
        self.assertEqual(len(t.calls), 0)

    def test_non_text_modality_no_network(self):
        c = cand()
        t = FakeTransport({"model": c.model_id, "output_text": SENTINEL})
        r = QualificationClient(transport=t).qualify(c, fleet_modality="image", route_kind="fixed", normalization_status="resolved")
        self.assertEqual(r.verdict, QualificationVerdict.REJECT)
        self.assertEqual(len(t.calls), 0)

    def test_embedding_modality_no_network(self):
        c = cand()
        t = FakeTransport({"model": c.model_id, "output_text": SENTINEL})
        r = QualificationClient(transport=t).qualify(c, fleet_modality="embedding", route_kind="fixed", normalization_status="resolved")
        self.assertEqual(r.verdict, QualificationVerdict.REJECT)
        self.assertEqual(len(t.calls), 0)

    def test_invalid_execution_path_no_network(self):
        c = cand(execution_path="DIRECT")
        t = FakeTransport({"model": c.model_id, "output_text": SENTINEL})
        r = QualificationClient(transport=t).qualify(c, fleet_modality="text", route_kind="fixed", normalization_status="resolved")
        self.assertEqual(r.verdict, QualificationVerdict.FAIL)
        self.assertEqual(len(t.calls), 0)
        self.assertTrue(r.evidence[0].binds_to(c))

    def test_invalid_execution_path_lowercase_no_network(self):
        c = cand(execution_path="omniroute")
        t = FakeTransport({"model": c.model_id, "output_text": SENTINEL})
        r = QualificationClient(transport=t).qualify(c, fleet_modality="text", route_kind="fixed", normalization_status="resolved")
        self.assertEqual(r.verdict, QualificationVerdict.FAIL)
        self.assertEqual(len(t.calls), 0)

    def test_rejection_is_before_network_pool(self):
        c = cand()
        t = FakeTransport({"model": c.model_id, "output_text": SENTINEL})
        r = QualificationClient(transport=t).qualify(c, fleet_modality="text", route_kind="pool", normalization_status="resolved")
        self.assertEqual(r.verdict, QualificationVerdict.REJECT)
        self.assertFalse(t.calls)


class D44CatalogSnapshotTests(unittest.TestCase):
    def test_catalog_unchanged_identity_and_length(self):
        before = copy.deepcopy(APPROVED_CATALOG)
        c = cand()
        run(QualificationClient(transport=FakeTransport({"model": c.model_id, "output_text": SENTINEL})), c)
        self.assertEqual(len(APPROVED_CATALOG), len(before))
        self.assertEqual(APPROVED_CATALOG, before)

    def test_catalog_mutation_detectable(self):
        before_ids = [r.model_id for r in APPROVED_CATALOG]
        before_len = len(APPROVED_CATALOG)
        c = cand(model="openai/gpt-5")
        run(QualificationClient(transport=FakeTransport({"model": c.model_id, "output_text": SENTINEL})), c)
        after_ids = [r.model_id for r in APPROVED_CATALOG]
        self.assertEqual(before_ids, after_ids)
        self.assertEqual(before_len, len(APPROVED_CATALOG))

    def test_no_approved_promotion(self):
        c = cand(model="openai/gpt-5")
        r = run(QualificationClient(transport=FakeTransport({"model": c.model_id, "output_text": SENTINEL})), c)
        self.assertEqual(r.verdict, QualificationVerdict.PASS)
        # candidate route must remain unapproved
        self.assertFalse(c.as_route().approved)
        self.assertFalse(any(x.approved for x in APPROVED_CATALOG if x.model_id == c.model_id))
        self.assertFalse(hasattr(r, "to_promoted_route"))

    def test_no_private_api_used(self):
        import pathlib
        src = pathlib.Path("nexus/routing/qualification_benchmark.py").read_text(encoding="utf-8")
        self.assertNotIn("_candidate_rejection_reasons", src)

    def test_no_uses_is_blocked_via_public_api(self):
        # benchmark should use public is_candidate_eligible not duplicate terra logic
        import pathlib
        src = pathlib.Path("nexus/routing/qualification_benchmark.py").read_text(encoding="utf-8")
        self.assertIn("is_candidate_eligible", src)




def test_default_qualification_timeout_is_bounded_and_propagated():
    from nexus.routing.qualification_benchmark import DEFAULT_TIMEOUT_SECONDS
    c = cand(model="openrouter/cohere/north-mini-code:free", provider="openrouter")
    transport = FakeTransport({"model": "cohere/north-mini-code:free", "output_text": SENTINEL})
    result = QualificationClient(transport=transport).qualify(
        c, fleet_modality="text", route_kind="fixed", normalization_status="resolved",
    )
    assert DEFAULT_TIMEOUT_SECONDS == 45.0
    assert transport.calls[0][3] == DEFAULT_TIMEOUT_SECONDS
    assert result.verdict == QualificationVerdict.PASS


def test_qualification_transport_timeout_remains_visible():
    c = cand(model="openrouter/cohere/north-mini-code:free", provider="openrouter")
    result = QualificationClient(transport=FakeTransport(error=TimeoutError())).qualify(
        c, fleet_modality="text", route_kind="fixed", normalization_status="resolved",
    )
    assert result.verdict == QualificationVerdict.FAIL
    assert "timeout" in result.evidence[0].detail

if __name__ == "__main__":
    unittest.main()
