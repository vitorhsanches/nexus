"""Nexus D.4.4 candidate qualification benchmark.

This is an additive, approval-independent live qualification boundary.  It
uses only the standard library and deliberately requires callers to provide
fleet/policy facts before any network request is made.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from nexus.routing.qualification_framework import (
    QualificationCandidate,
    QualificationEvidence,
    QualificationEvidenceType,
    QualificationRecord,
    QualificationVerdict,
    evaluate_qualification,
    is_candidate_eligible,
)

DEFAULT_BASE_URL = "http://127.0.0.1:20128"
DEFAULT_TIMEOUT_SECONDS = 10.0
SENTINEL = "NEXUS_D44_SENTINEL_7F3A"
PROBE = f"Reply with exactly this text and nothing else: {SENTINEL}"


class QualificationTransport(Protocol):
    def post_json(self, url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> Any: ...


class QualificationError(RuntimeError):
    """Safe, secret-free qualification failure."""


class StdlibQualificationTransport:
    def post_json(self, url, headers, payload, timeout):
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if not 200 <= response.status < 300:
                    raise QualificationError(f"HTTP status {response.status}")
                raw = response.read()
        except QualificationError:
            raise
        except urllib.error.HTTPError as exc:
            raise QualificationError(f"HTTP status {exc.code}") from None
        except TimeoutError:
            raise QualificationError("transport timeout") from None
        except OSError:
            raise QualificationError("transport failure") from None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            raise QualificationError("malformed JSON response") from None


@dataclass(frozen=True, slots=True)
class QualificationClient:
    base_url: str = DEFAULT_BASE_URL
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    api_key: str | None = None
    transport: QualificationTransport | None = None

    def __repr__(self) -> str:
        return f"QualificationClient(base_url={self.base_url!r}, timeout={self.timeout!r}, api_key=<redacted>)"

    def qualify(self, candidate: QualificationCandidate, *, fleet_modality: str | None,
                route_kind: str | None, normalization_status: str | None,
                policy_blocked: bool = False) -> QualificationRecord:
        identity = dict(model_id=candidate.model_id, provider=candidate.provider,
                        execution_path=candidate.execution_path, effort=candidate.effort,
                        capability=candidate.capability, risk=candidate.risk)
        if candidate.execution_path != "OMNIROUTE":
            return self._failed(candidate, identity, "invalid execution_path")
        if not is_candidate_eligible(candidate, fleet_modality=fleet_modality, route_kind=route_kind,
                                      normalization_status=normalization_status, policy_blocked=policy_blocked):
            rec = evaluate_qualification(candidate, (), fleet_modality=fleet_modality, route_kind=route_kind,
                                         normalization_status=normalization_status, policy_blocked=policy_blocked)
            return rec
        try:
            key = self.api_key if self.api_key is not None else os.environ.get("OMNIROUTE_API_KEY")
            headers = {"Accept": "application/json", "Content-Type": "application/json"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            transport = self.transport or StdlibQualificationTransport()
            payload: dict[str, Any] = {"model": candidate.model_id, "input": PROBE, "metadata": {"nexus": "D4.4"}}
            if candidate.effort:
                payload["reasoning"] = {"effort": candidate.effort}
            response = transport.post_json(self.base_url.rstrip("/") + "/v1/responses", headers, payload, self.timeout)
            if not isinstance(response, dict):
                raise QualificationError("malformed JSON response")
        except QualificationError as exc:
            return self._failed(candidate, identity, str(exc))
        except TimeoutError:
            return self._failed(candidate, identity, "transport timeout")
        except OSError:
            return self._failed(candidate, identity, "transport failure")
        except Exception:
            return self._failed(candidate, identity, "transport failure")
        returned_model = response.get("model")
        text = _response_text(response)
        exact_pass = isinstance(returned_model, str) and returned_model == candidate.model_id
        exact_detail = f"exact model identity: requested={candidate.model_id!r} returned={returned_model!r}"
        exact_meta = {"returned_model": returned_model}
        evidence = (
            QualificationEvidence(QualificationEvidenceType.EXACT_IDENTITY_CHECK, "responses", exact_pass, exact_detail, meta=exact_meta, **identity),
            QualificationEvidence(QualificationEvidenceType.HOST_VALIDATION, "responses", True, "valid JSON response", **identity),
            QualificationEvidence(QualificationEvidenceType.SEMANTIC_REVIEW, "responses", SENTINEL in text, "deterministic sentinel probe", **identity),
        )
        return evaluate_qualification(candidate, evidence, fleet_modality=fleet_modality, route_kind=route_kind, normalization_status=normalization_status, policy_blocked=policy_blocked)

    @staticmethod
    def _failed(candidate, identity, error):
        safe = _redact(str(error))
        ev = QualificationEvidence(QualificationEvidenceType.HOST_VALIDATION, "responses", False, safe, **identity)
        return QualificationRecord(candidate, QualificationVerdict.FAIL, (ev,))


def _redact(msg: str) -> str:
    low = msg.lower()
    if "bearer" in low or "api_key" in low or "sk-" in msg:
        return "transport failure"
    return msg


def _response_text(response: dict[str, Any]) -> str:
    parts: list[str] = []
    ot = response.get("output_text")
    if isinstance(ot, str) and ot:
        parts.append(ot)
    output = response.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    for key in ("text", "output_text"):
                        v = c.get(key)
                        if isinstance(v, str) and v:
                            parts.append(v)
            for key in ("text", "output_text"):
                v = item.get(key)
                if isinstance(v, str) and v:
                    parts.append(v)
    return " ".join(parts)


def qualify_candidate(candidate: QualificationCandidate, **kwargs) -> QualificationRecord:
    return QualificationClient().qualify(candidate, **kwargs)
