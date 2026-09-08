"""Nexus v2.0-D.4.3 Generic Model Qualification Framework tests."""
import unittest
from nexus.routing.catalog import APPROVED_CATALOG, is_blocked_model
from nexus.routing.fleet import Modality, NormalizationStatus, RouteKind, infer_modality_from_model_id, infer_route_kind, normalize_discovered_fleet
from nexus.routing.models import CapabilityClass, ModelRoute, RiskLevel
from nexus.routing.qualification_framework import (
    QualificationCandidate, QualificationEvidence, QualificationEvidenceType, QualificationRecord, QualificationVerdict, RejectionReason,
    _candidate_rejection_reasons, _mandatory_evidence_satisfied, evaluate_qualification, is_candidate_eligible,
)
from nexus.routing.telemetry import DiscoveredModel
import inspect

def _candidate(model_id="cc/claude-sonnet-5-high", provider="claude", execution_path="OMNIROUTE", effort="high", capability="review", risk="high"):
    return QualificationCandidate(model_id=model_id, provider=provider, execution_path=execution_path, effort=effort, capability=capability, risk=risk)

def _evidence_for(candidate, evidence_type, passed=True):
    return QualificationEvidence(evidence_type=evidence_type, source="test", passed=passed, detail="detail", model_id=candidate.model_id, provider=candidate.provider, execution_path=candidate.execution_path, effort=candidate.effort, capability=candidate.capability, risk=candidate.risk)

def _pass_evidence(candidate):
    return (
        _evidence_for(candidate, QualificationEvidenceType.EXACT_IDENTITY_CHECK, True),
        _evidence_for(candidate, QualificationEvidenceType.HOST_VALIDATION, True),
        _evidence_for(candidate, QualificationEvidenceType.SEMANTIC_REVIEW, True),
    )

OK_NS = NormalizationStatus.RESOLVED.value
OK_KIND = RouteKind.FIXED.value

class VerdictEnumTests(unittest.TestCase):
    def test_verdict_values(self):
        self.assertEqual(QualificationVerdict.PASS.value, "pass")
        self.assertEqual(QualificationVerdict.FAIL.value, "fail")
        self.assertEqual(QualificationVerdict.REJECT.value, "reject")

class CentralizedBlockingTests(unittest.TestCase):
    def test_uses_is_blocked_model(self):
        src = inspect.getsource(evaluate_qualification)
        # also check _candidate_rejection_reasons
        src2 = inspect.getsource(_candidate_rejection_reasons)
        combined = src + src2
        self.assertIn("is_blocked_model", combined)
        # ensure no custom regex for terra
        self.assertNotIn("gpt56terra", open("nexus/routing/qualification_framework.py").read().lower().replace("is_blocked_model",""))
        self.assertTrue(is_blocked_model("gpt-5.6-terra"))
        self.assertTrue(is_blocked_model("GPT_5_6_TERRA"))

    def test_no_promoted_helper(self):
        self.assertFalse(hasattr(QualificationRecord, "to_promoted_route"))
        src = open("nexus/routing/qualification_framework.py").read()
        self.assertNotIn("to_promoted_route", src)
        self.assertNotIn("approved=True", src)

    def test_pass_cannot_create_production_eligible(self):
        c = _candidate()
        rec = evaluate_qualification(c, _pass_evidence(c), route_kind=OK_KIND, normalization_status=OK_NS)
        self.assertEqual(rec.verdict, QualificationVerdict.PASS)
        # as_route must be approved=False experimental=True
        route = c.as_route()
        self.assertFalse(route.approved)
        self.assertTrue(route.experimental)
        # no way to get approved route from record
        self.assertFalse(hasattr(rec, "to_promoted_route"))

class NoNetworkTests(unittest.TestCase):
    def test_no_network_imports(self):
        src = open("nexus/routing/qualification_framework.py").read()
        for kw in ["requests", "http", "urllib", "socket"]:
            self.assertNotIn(kw, src.lower())

class EvidenceBindingTests(unittest.TestCase):
    def test_model_identity_isolation(self):
        cA = _candidate(model_id="cc/claude-sonnet-5-high")
        cB = _candidate(model_id="cc/claude-sonnet-5-low")
        ev = _pass_evidence(cA)  # bound to A
        recB = evaluate_qualification(cB, ev, route_kind=OK_KIND, normalization_status=OK_NS)
        self.assertEqual(recB.verdict, QualificationVerdict.FAIL)

    def test_provider_isolation(self):
        cA = _candidate(provider="claude")
        cB = _candidate(provider="openai")
        ev = _pass_evidence(cA)
        recB = evaluate_qualification(cB, ev, route_kind=OK_KIND, normalization_status=OK_NS)
        self.assertEqual(recB.verdict, QualificationVerdict.FAIL)

    def test_execution_path_isolation(self):
        cA = _candidate(execution_path="OMNIROUTE")
        cB = _candidate(execution_path="DIRECT")
        ev = _pass_evidence(cA)
        recB = evaluate_qualification(cB, ev, route_kind=OK_KIND, normalization_status=OK_NS)
        self.assertEqual(recB.verdict, QualificationVerdict.FAIL)

    def test_effort_isolation(self):
        cA = _candidate(effort="high")
        cB = _candidate(effort="low")
        ev = _pass_evidence(cA)
        recB = evaluate_qualification(cB, ev, route_kind=OK_KIND, normalization_status=OK_NS)
        self.assertEqual(recB.verdict, QualificationVerdict.FAIL)

    def test_capability_isolation(self):
        cA = _candidate(capability="review")
        cB = _candidate(capability="advanced-coding")
        ev = _pass_evidence(cA)
        recB = evaluate_qualification(cB, ev, route_kind=OK_KIND, normalization_status=OK_NS)
        self.assertEqual(recB.verdict, QualificationVerdict.FAIL)

    def test_risk_isolation(self):
        cA = _candidate(risk="high")
        cB = _candidate(risk="medium")
        ev = _pass_evidence(cA)
        recB = evaluate_qualification(cB, ev, route_kind=OK_KIND, normalization_status=OK_NS)
        self.assertEqual(recB.verdict, QualificationVerdict.FAIL)

    def test_contradictory_mandatory_evidence_fails(self):
        c = _candidate()
        ev = (
            _evidence_for(c, QualificationEvidenceType.EXACT_IDENTITY_CHECK, True),
            _evidence_for(c, QualificationEvidenceType.EXACT_IDENTITY_CHECK, False),
            _evidence_for(c, QualificationEvidenceType.HOST_VALIDATION, True),
            _evidence_for(c, QualificationEvidenceType.SEMANTIC_REVIEW, True),
        )
        rec = evaluate_qualification(c, ev, route_kind=OK_KIND, normalization_status=OK_NS)
        self.assertEqual(rec.verdict, QualificationVerdict.FAIL)

    def test_contradictory_host_validation_fails(self):
        c = _candidate()
        ev = (
            _evidence_for(c, QualificationEvidenceType.EXACT_IDENTITY_CHECK, True),
            _evidence_for(c, QualificationEvidenceType.HOST_VALIDATION, True),
            _evidence_for(c, QualificationEvidenceType.HOST_VALIDATION, False),
            _evidence_for(c, QualificationEvidenceType.SEMANTIC_REVIEW, True),
        )
        rec = evaluate_qualification(c, ev, route_kind=OK_KIND, normalization_status=OK_NS)
        self.assertEqual(rec.verdict, QualificationVerdict.FAIL)

class SafetyMetadataTests(unittest.TestCase):
    def test_unresolved_normalization_rejects(self):
        c = _candidate()
        rec = evaluate_qualification(c, _pass_evidence(c), route_kind=OK_KIND, normalization_status=NormalizationStatus.UNRESOLVED.value)
        self.assertEqual(rec.verdict, QualificationVerdict.REJECT)
        self.assertIn(RejectionReason.UNRESOLVED, rec.rejection_reasons)

    def test_omitted_normalization_fails_closed(self):
        c = _candidate()
        rec = evaluate_qualification(c, _pass_evidence(c), route_kind=OK_KIND)
        self.assertNotEqual(rec.verdict, QualificationVerdict.PASS)
        self.assertEqual(rec.verdict, QualificationVerdict.REJECT)

    def test_omitted_route_kind_fails_closed(self):
        c = _candidate()
        rec = evaluate_qualification(c, _pass_evidence(c), normalization_status=OK_NS)
        self.assertNotEqual(rec.verdict, QualificationVerdict.PASS)
        self.assertEqual(rec.verdict, QualificationVerdict.REJECT)

    def test_omitted_both_fails_closed(self):
        c = _candidate()
        rec = evaluate_qualification(c, _pass_evidence(c))
        self.assertNotEqual(rec.verdict, QualificationVerdict.PASS)
        self.assertEqual(rec.verdict, QualificationVerdict.REJECT)

    def test_pool_rejects(self):
        c = _candidate()
        rec = evaluate_qualification(c, _pass_evidence(c), route_kind=RouteKind.POOL.value, normalization_status=OK_NS)
        self.assertEqual(rec.verdict, QualificationVerdict.REJECT)
        self.assertIn(RejectionReason.POOL, rec.rejection_reasons)

    def test_unknown_rejects(self):
        c = _candidate()
        rec = evaluate_qualification(c, _pass_evidence(c), route_kind=RouteKind.UNKNOWN.value, normalization_status=OK_NS)
        self.assertEqual(rec.verdict, QualificationVerdict.REJECT)
        self.assertIn(RejectionReason.UNKNOWN_ROUTE_KIND, rec.rejection_reasons)

class ModalityAndFreeRouteTests(unittest.TestCase):
    def test_fixed_free_route_qualifies(self):
        c = _candidate(model_id="oc/mimo-v2.5-free", provider="opencode", capability="mechanical", risk="low", effort="low")
        rec = evaluate_qualification(c, _pass_evidence(c), route_kind=RouteKind.FIXED.value, normalization_status=OK_NS, fleet_modality=Modality.UNRESOLVED.value)
        self.assertEqual(rec.verdict, QualificationVerdict.PASS)

    def test_unresolved_modality_accepted_for_fixed_llm(self):
        c = _candidate()
        rec = evaluate_qualification(c, _pass_evidence(c), route_kind=OK_KIND, normalization_status=OK_NS, fleet_modality=Modality.UNRESOLVED.value)
        self.assertEqual(rec.verdict, QualificationVerdict.PASS)

    def test_embedding_rejects(self):
        c = _candidate(model_id="text-embedding-3-large")
        rec = evaluate_qualification(c, _pass_evidence(c), route_kind=OK_KIND, normalization_status=OK_NS, fleet_modality=Modality.EMBEDDING.value)
        self.assertEqual(rec.verdict, QualificationVerdict.REJECT)

    def test_image_rejects(self):
        c = _candidate()
        rec = evaluate_qualification(c, _pass_evidence(c), route_kind=OK_KIND, normalization_status=OK_NS, fleet_modality=Modality.IMAGE.value)
        self.assertEqual(rec.verdict, QualificationVerdict.REJECT)

    def test_audio_rejects(self):
        c = _candidate()
        rec = evaluate_qualification(c, _pass_evidence(c), route_kind=OK_KIND, normalization_status=OK_NS, fleet_modality=Modality.AUDIO.value)
        self.assertEqual(rec.verdict, QualificationVerdict.REJECT)

    def test_rerank_rejects(self):
        c = _candidate()
        rec = evaluate_qualification(c, _pass_evidence(c), route_kind=OK_KIND, normalization_status=OK_NS, fleet_modality=Modality.RERANK.value)
        self.assertEqual(rec.verdict, QualificationVerdict.REJECT)

    def test_safety_rejects(self):
        c = _candidate()
        rec = evaluate_qualification(c, _pass_evidence(c), route_kind=OK_KIND, normalization_status=OK_NS, fleet_modality=Modality.SAFETY.value)
        self.assertEqual(rec.verdict, QualificationVerdict.REJECT)

    def test_terra_rejects_via_is_blocked(self):
        c = _candidate(model_id="gpt-5.6-terra")
        rec = evaluate_qualification(c, _pass_evidence(c), route_kind=OK_KIND, normalization_status=OK_NS)
        self.assertEqual(rec.verdict, QualificationVerdict.REJECT)
        self.assertIn(RejectionReason.TERRA, rec.rejection_reasons)

class VocabularyTests(unittest.TestCase):
    def test_unknown_capability_fails_closed(self):
        c = _candidate(capability="nonsense-cap")
        rec = evaluate_qualification(c, _pass_evidence(c), route_kind=OK_KIND, normalization_status=OK_NS)
        self.assertEqual(rec.verdict, QualificationVerdict.FAIL)

    def test_unknown_risk_fails_closed(self):
        c = _candidate(risk="extreme")
        rec = evaluate_qualification(c, _pass_evidence(c), route_kind=OK_KIND, normalization_status=OK_NS)
        self.assertEqual(rec.verdict, QualificationVerdict.FAIL)

    def test_mandatory_missing_fails(self):
        c = _candidate()
        rec = evaluate_qualification(c, (), route_kind=OK_KIND, normalization_status=OK_NS)
        self.assertEqual(rec.verdict, QualificationVerdict.FAIL)

class CatalogUnchangedTests(unittest.TestCase):
    def test_catalog_unchanged(self):
        before = list(APPROVED_CATALOG)
        c = _candidate()
        evaluate_qualification(c, _pass_evidence(c), route_kind=OK_KIND, normalization_status=OK_NS)
        self.assertEqual(before, list(APPROVED_CATALOG))
        self.assertEqual(len(APPROVED_CATALOG), 4)

if __name__ == "__main__":
    unittest.main()
