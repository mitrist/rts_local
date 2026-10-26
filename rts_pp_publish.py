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
    file_hashes: str | list[Any] | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """POST .../OfferPriceListItem/prepareforpublish."""
    body: dict[str, Any] = {"offerPriceListItemId": int(draft_id)}
    # LK often sends only offerPriceListItemId; keep optional fileHashes for callers.
    if file_hashes is not None:
        if isinstance(file_hashes, list):
            body["fileHashes"] = json.dumps(file_hashes, ensure_ascii=False)
        else:
            body["fileHashes"] = str(file_hashes)
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
    # Response usually has only stringForSign/fileHashes — keep draft id for publish.
    data.setdefault("offerPriceListItemId", int(draft_id))
    return data


def append_signed_data(
    cookie: str,
    signatures: list[dict[str, Any]],
    *,
    sign_requests: list[dict[str, Any]] | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """POST platform TSA append/signed-data.

    RTS expects an object::
        {"signatureWithSignedDatas": [
            {"fileGuid", "signature", "signedData", "isDetached", "isHash"}, ...
        ]}
    Response is a JSON array of ``{fileGuid, signatureWithTimeStamp}``.
    """
    if not isinstance(signatures, list) or not signatures:
        raise RtsPpApiError("Нужен непустой массив signatures.", reason="bad_request")

    payload = build_tsa_request_body(signatures, sign_requests=sign_requests)
    try:
        import requests

        resp = requests.post(
            PLATFORM_TSA_URL,
            headers=platform_registry_headers(cookie),
            json=payload,
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
        # Fallback: pass through already-stamped CMS if TSA returned empty.
        return [
            {
                "fileGuid": s.get("fileGuid"),
                "signatureWithTimeStamp": s.get("signatureWithTimeStamp") or s.get("signature") or "",
            }
            for s in signatures
            if isinstance(s, dict)
        ]
    try:
        data = resp.json()
    except ValueError as exc:
        raise RtsPpApiError("TSA вернул не-JSON.", reason="api_error") from exc
    return parse_tsa_response(data, fallback_signatures=signatures)


def build_tsa_request_body(
    signatures: list[dict[str, Any]],
    *,
    sign_requests: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Wrap CMS signatures into AppendSignatureTimeStampBySignedDatasRequest."""
    items: list[dict[str, Any]] = []
    reqs = sign_requests if isinstance(sign_requests, list) else []
    for idx, sig in enumerate(signatures):
        if not isinstance(sig, dict):
            raise RtsPpApiError("Каждая подпись должна быть объектом.", reason="bad_request")
        cms = str(
            sig.get("signature")
            or sig.get("signatureWithTimeStamp")
            or sig.get("cms")
            or ""
        ).strip()
        cms = "".join(cms.split())
        if not cms:
            raise RtsPpApiError("Пустая подпись для TSA.", reason="bad_request")

        req = reqs[idx] if idx < len(reqs) and isinstance(reqs[idx], dict) else {}
        file_guid = sig.get("fileGuid") if "fileGuid" in sig else sig.get("file_guid")
        if file_guid is None and req:
            file_guid = req.get("file_guid")

        signed_data = sig.get("signedData")
        if signed_data is None or signed_data == "":
            signed_data = req.get("data") if req else None
        signed_data = str(signed_data or "").strip()
        if not signed_data:
            raise RtsPpApiError(
                "Для TSA нужен signedData (исходные данные подписи).",
                reason="bad_request",
            )

        encoding = str(sig.get("encoding") or (req.get("encoding") if req else "") or "").strip()
        is_hash = bool(sig.get("isHash")) if "isHash" in sig else encoding == "hex_hash"
        is_detached = bool(sig["isDetached"]) if "isDetached" in sig else True

        items.append(
            {
                "fileGuid": file_guid,
                "signature": cms,
                "signedData": signed_data,
                "isDetached": is_detached,
                "isHash": is_hash,
            }
        )
    return {"signatureWithSignedDatas": items}


def parse_tsa_response(
    data: Any,
    *,
    fallback_signatures: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Normalize TSA response to publish ``[{fileGuid, signatureWithTimeStamp}]``."""
    if isinstance(data, dict):
        # Some gateways wrap the array.
        for key in ("signatureWithSignedDatas", "signatures", "items", "result"):
            inner = data.get(key)
            if isinstance(inner, list):
                data = inner
                break
    if not isinstance(data, list):
        if fallback_signatures:
            return [
                {
                    "fileGuid": s.get("fileGuid"),
                    "signatureWithTimeStamp": s.get("signatureWithTimeStamp")
                    or s.get("signature")
                    or "",
                }
                for s in fallback_signatures
                if isinstance(s, dict)
            ]
        raise RtsPpApiError("TSA вернул неожиданный формат.", reason="api_error", detail=data)

    out: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        stamped = str(
            item.get("signatureWithTimeStamp") or item.get("signature") or ""
        ).strip()
        stamped = "".join(stamped.split())
        if not stamped:
            continue
        file_guid = item.get("fileGuid") if "fileGuid" in item else item.get("file_guid")
        out.append({"fileGuid": file_guid, "signatureWithTimeStamp": stamped})
    if not out:
        raise RtsPpApiError("TSA не вернул подписи с меткой времени.", reason="api_error", detail=data)
    return out


def publish_offer(
    cookie: str,
    prepare_result: dict[str, Any],
    signatures: list[dict[str, Any]],
    *,
    draft_id: int | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Any:
    """POST .../OfferPriceListItem/publish.

    Real LK body (not prepare echo)::
        {
          "usingParticipantInvolvingProcess": false,
          "offerPriceListItemId": <id>,
          "signedData": {
            "signature": "<card CMS with TSA>",
            "fileSignatures": [
              {"Guid": "...", "Hash": "...", "DigitalSign": "<file CMS with TSA>"}
            ]
          }
        }
    """
    body = build_publish_body(prepare_result, signatures, draft_id=draft_id)
    return _request_json(
        "POST",
        f"{OFFERS_BASE}/publish",
        cookie=cookie,
        json_body=body,
        timeout=timeout,
    )


def build_publish_body(
    prepare_result: dict[str, Any] | None,
    signatures: list[dict[str, Any]],
    *,
    draft_id: int | None = None,
) -> dict[str, Any]:
    """Build OfferPriceListItem/publish JSON from TSA-stamped signatures."""
    prep = prepare_result if isinstance(prepare_result, dict) else {}
    oid = draft_id
    if oid is None:
        raw = prep.get("offerPriceListItemId") or prep.get("OfferPriceListItemId")
        if raw is not None and str(raw).strip():
            oid = int(raw)
    if oid is None:
        raise RtsPpApiError(
            "Не задан offerPriceListItemId для publish.",
            reason="bad_request",
        )

    hash_by_guid: dict[str, str] = {}
    for fh in _parse_file_hashes(prep.get("fileHashes")):
        hash_by_guid[str(fh["Guid"])] = str(fh["Hash"])

    card_cms = ""
    file_sigs: list[dict[str, Any]] = []
    if not isinstance(signatures, list) or not signatures:
        raise RtsPpApiError("Нужны подписи для publish.", reason="bad_request")

    for sig in signatures:
        if not isinstance(sig, dict):
            continue
        cms = str(
            sig.get("signatureWithTimeStamp") or sig.get("signature") or sig.get("DigitalSign") or ""
        ).strip()
        cms = "".join(cms.split())
        if not cms:
            continue
        file_guid = sig.get("fileGuid") if "fileGuid" in sig else sig.get("file_guid")
        if file_guid is None:
            file_guid = sig.get("Guid")
        if file_guid is None or str(file_guid).strip() == "":
            card_cms = cms
            continue
        guid = str(file_guid).strip()
        file_sigs.append(
            {
                "Guid": guid,
                "Hash": hash_by_guid.get(guid) or str(sig.get("Hash") or sig.get("hash") or ""),
                "DigitalSign": cms,
            }
        )

    if not card_cms:
        raise RtsPpApiError(
            "Нет подписи карточки ПП (fileGuid=null) для publish.",
            reason="bad_request",
        )

    return {
        "usingParticipantInvolvingProcess": bool(prep.get("usingParticipantInvolvingProcess", False)),
        "offerPriceListItemId": int(oid),
        "signedData": {
            "signature": card_cms,
            "fileSignatures": file_sigs,
        },
    }


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
