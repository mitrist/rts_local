"""RTS OfferPriceListItem publish API (prepare → TSA → publish)."""

from __future__ import annotations

import json
from typing import Any

from rts_pp_api import OFFERS_BASE, RtsPpApiError, _request_json, platform_registry_headers

PLATFORM_TSA_URL = (
    "https://platform.rts-tender.ru/signature-time-stamp/api/v1/"
    "signature-time-stamps/append/signed-data"
)

_DEFAULT_TIMEOUT = 90.0


def prepare_for_publish(
    cookie: str,
    draft_id: int,
    *,
    file_hashes: str | list[Any] = "[]",
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """POST .../OfferPriceListItem/prepareforpublish."""
    if isinstance(file_hashes, list):
        file_hashes_str = json.dumps(file_hashes, ensure_ascii=False)
    else:
        file_hashes_str = str(file_hashes if file_hashes is not None else "[]")
    body = {
        "offerPriceListItemId": int(draft_id),
        "fileHashes": file_hashes_str,
    }
    data = _request_json(
        "POST",
        f"{OFFERS_BASE}/prepareforpublish",
        cookie=cookie,
        json_body=body,
        timeout=timeout,
    )
    if not isinstance(data, dict):
        raise RtsPpApiError(
            "prepareforpublish вернул не-объект.",
            reason="api_error",
            detail=data,
        )
    return data


def append_signed_data(
    cookie: str,
    signatures: list[dict[str, Any]],
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Any:
    """POST platform TSA append/signed-data."""
    if not isinstance(signatures, list) or not signatures:
        raise RtsPpApiError("Нужен непустой массив signatures.", reason="bad_request")
    try:
        import requests

        resp = requests.post(
            PLATFORM_TSA_URL,
            headers=platform_registry_headers(cookie),
            json=signatures,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 — surface as API error
        raise RtsPpApiError(f"Сеть TSA: {exc}", reason="api_error") from exc
    if resp.status_code in (401, 403):
        raise RtsPpApiError(
            "Сессия РТС истекла (TSA). Обновите cookie через расширение.",
            reason="session_expired",
            detail={"http_status": resp.status_code},
        )
    if resp.status_code != 200:
        raise RtsPpApiError(
            f"TSA API {resp.status_code}: {(resp.text or '')[:500]}",
            reason="api_error",
            detail={"http_status": resp.status_code},
        )
    text = (resp.text or "").strip()
    if not text:
        return signatures
    try:
        return resp.json()
    except ValueError:
        return signatures


def publish_offer(
    cookie: str,
    prepare_result: dict[str, Any],
    signatures: list[dict[str, Any]],
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Any:
    """POST .../OfferPriceListItem/publish with prepare body + signatures."""
    body = dict(prepare_result)
    body["signatures"] = signatures
    return _request_json(
        "POST",
        f"{OFFERS_BASE}/publish",
        cookie=cookie,
        json_body=body,
        timeout=timeout,
    )


def _parse_file_hashes(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if not text or text == "[]":
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return []
        items = parsed if isinstance(parsed, list) else []
    else:
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        guid = item.get("Guid") if "Guid" in item else item.get("guid")
        hash_val = item.get("Hash") if "Hash" in item else item.get("hash")
        if not guid or not hash_val:
            continue
        out.append({"Guid": str(guid), "Hash": str(hash_val)})
    return out


def build_sign_requests(prepare_result: dict[str, Any]) -> list[dict[str, Any]]:
    """Build CryptoPro sign tasks from prepareforpublish response.

    Order matches RTS dumps: file hashes first (if any), then card (fileGuid null).
    """
    if not isinstance(prepare_result, dict):
        raise RtsPpApiError("prepare_result должен быть объектом.", reason="bad_request")

    string_for_sign = (
        prepare_result.get("stringForSign") or prepare_result.get("StringForSign") or ""
    )
    string_for_sign = str(string_for_sign).strip()
    if not string_for_sign:
        raise RtsPpApiError(
            "prepareforpublish не вернул stringForSign.",
            reason="api_error",
            detail=prepare_result,
        )

    requests_out: list[dict[str, Any]] = []
    file_hashes = _parse_file_hashes(prepare_result.get("fileHashes"))
    for fh in file_hashes:
        requests_out.append(
            {
                "kind": "file_hash",
                "file_guid": fh["Guid"],
                "encoding": "hex_hash",
                "data": fh["Hash"],
                "label": f"Файл {fh['Guid'][:8]}…",
            }
        )
    requests_out.append(
        {
            "kind": "card",
            "file_guid": None,
            "encoding": "base64",
            "data": string_for_sign,
            "label": "Карточка ПП",
        }
    )
    return requests_out


def normalize_submitted_signatures(
    sign_requests: list[dict[str, Any]],
    submitted: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Map UI signatures to RTS [{fileGuid, signatureWithTimeStamp}, ...]."""
    if not isinstance(submitted, list) or len(submitted) != len(sign_requests):
        raise RtsPpApiError(
            f"Ожидалось {len(sign_requests)} подписей, получено "
            f"{len(submitted) if isinstance(submitted, list) else 0}.",
            reason="bad_request",
        )
    out: list[dict[str, Any]] = []
    for req, sig in zip(sign_requests, submitted):
        if not isinstance(sig, dict):
            raise RtsPpApiError("Каждая подпись должна быть объектом.", reason="bad_request")
        cms = (
            sig.get("signatureWithTimeStamp")
            or sig.get("signature")
            or sig.get("cms")
            or ""
        )
        cms = str(cms).strip()
        if not cms:
            raise RtsPpApiError("Пустая подпись ЭП.", reason="bad_request")
        # Strip PEM / whitespace if operator pasted accidentally
        cms = "".join(cms.split())
        if cms.startswith("-----BEGIN"):
            raise RtsPpApiError(
                "Ожидается CMS/CAdES в Base64 без PEM-обёртки.",
                reason="bad_request",
            )
        file_guid = sig.get("fileGuid") if "fileGuid" in sig else sig.get("file_guid")
        if file_guid is None:
            file_guid = req.get("file_guid")
        out.append(
            {
                "fileGuid": file_guid,
                "signatureWithTimeStamp": cms,
            }
        )
    return out
