"""Unit tests for PP publish prepare → sign_requests parsing and jobs."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from eis_analytics_db import (
    create_pp_publish_job,
    find_pp_publish_job_for_draft,
    get_pp_publish_job,
    update_pp_publish_job,
)
from rts_pp_publish import build_sign_requests, normalize_submitted_signatures
from rts_pp_api import RtsPpApiError
from rts_pp_publish_service import start_publish_job, submit_publish_signatures


def test_build_sign_requests_card_only():
    prep = {
        "stringForSign": "PD94bWwgdmVyc2lvbj0iMS4wIj8+",
        "fileHashes": "[]",
    }
    reqs = build_sign_requests(prep)
    assert len(reqs) == 1
    assert reqs[0]["kind"] == "card"
    assert reqs[0]["file_guid"] is None
    assert reqs[0]["encoding"] == "base64"
    assert reqs[0]["data"] == prep["stringForSign"]


def test_build_sign_requests_with_file_hashes():
    prep = {
        "stringForSign": "AAA=",
        "fileHashes": json.dumps(
            [{"Guid": "e57f6890-183a-4c8c-9068-bdc4c147e59e", "Hash": "ABCD" * 16}]
        ),
    }
    reqs = build_sign_requests(prep)
    assert len(reqs) == 2
    assert reqs[0]["kind"] == "file_hash"
    assert reqs[0]["file_guid"] == "e57f6890-183a-4c8c-9068-bdc4c147e59e"
    assert reqs[1]["kind"] == "card"
    assert reqs[1]["file_guid"] is None


def test_build_sign_requests_keeps_duplicate_file_guids():
    same = "3CD24E70D72D9CFF5587154AD14991EF9181678E6B8780C732C1FF10A1D5F5E8"
    hashes = [{"Guid": f"00000000-0000-0000-0000-{i:012d}", "Hash": same} for i in range(80)]
    reqs = build_sign_requests({"stringForSign": "AAA=", "fileHashes": json.dumps(hashes)})
    assert len(reqs) == 81
    assert {r["data"] for r in reqs if r["kind"] == "file_hash"} == {same}
    assert reqs[-1]["kind"] == "card"


def test_build_sign_requests_missing_string():
    with pytest.raises(RtsPpApiError):
        build_sign_requests({"fileHashes": "[]"})


def test_build_publish_body_matches_lk_contract():
    from rts_pp_publish import build_publish_body

    prep = {
        "stringForSign": "PD94bWw+",
        "fileHashes": json.dumps(
            [{"Guid": "e57f6890-183a-4c8c-9068-bdc4c147e59e", "Hash": "ABCD" * 16}]
        ),
    }
    stamped = [
        {
            "fileGuid": "e57f6890-183a-4c8c-9068-bdc4c147e59e",
            "signatureWithTimeStamp": "FILECMS",
        },
        {"fileGuid": None, "signatureWithTimeStamp": "CARDCMS"},
    ]
    body = build_publish_body(prep, stamped, draft_id=560425)
    assert body == {
        "usingParticipantInvolvingProcess": False,
        "offerPriceListItemId": 560425,
        "signedData": {
            "signature": "CARDCMS",
            "fileSignatures": [
                {
                    "Guid": "e57f6890-183a-4c8c-9068-bdc4c147e59e",
                    "Hash": "ABCD" * 16,
                    "DigitalSign": "FILECMS",
                }
            ],
        },
    }


def test_build_publish_body_requires_card_signature():
    from rts_pp_publish import build_publish_body

    with pytest.raises(RtsPpApiError):
        build_publish_body(
            {"fileHashes": "[]"},
            [{"fileGuid": "aaa", "signatureWithTimeStamp": "X"}],
            draft_id=1,
        )


def test_normalize_submitted_signatures():
    reqs = build_sign_requests({"stringForSign": "QQ==", "fileHashes": "[]"})
    out = normalize_submitted_signatures(
        reqs,
        [{"signature": "MIIGsig=="}],
    )
    assert out == [{"fileGuid": None, "signatureWithTimeStamp": "MIIGsig=="}]


def test_build_tsa_request_body_wraps_object():
    from rts_pp_publish import build_tsa_request_body

    reqs = build_sign_requests(
        {
            "stringForSign": "PD94bWw+",
            "fileHashes": json.dumps(
                [{"Guid": "e57f6890-183a-4c8c-9068-bdc4c147e59e", "Hash": "ABCD" * 16}]
            ),
        }
    )
    cms = [{"signatureWithTimeStamp": "CMSfile"}, {"signatureWithTimeStamp": "CMScard"}]
    # normalize first so fileGuid is set
    from rts_pp_publish import normalize_submitted_signatures

    normalized = normalize_submitted_signatures(reqs, cms)
    body = build_tsa_request_body(normalized, sign_requests=reqs)
    assert list(body.keys()) == ["signatureWithSignedDatas"]
    items = body["signatureWithSignedDatas"]
    assert len(items) == 2
    assert items[0] == {
        "fileGuid": "e57f6890-183a-4c8c-9068-bdc4c147e59e",
        "signature": "CMSfile",
        "signedData": "ABCD" * 16,
        "isDetached": True,
        "isHash": True,
    }
    assert items[1] == {
        "fileGuid": None,
        "signature": "CMScard",
        "signedData": "PD94bWw+",
        "isDetached": True,
        "isHash": False,
    }


def test_parse_tsa_response_array():
    from rts_pp_publish import parse_tsa_response

    out = parse_tsa_response(
        [{"fileGuid": None, "signatureWithTimeStamp": "STAMPED"}]
    )
    assert out == [{"fileGuid": None, "signatureWithTimeStamp": "STAMPED"}]


def test_append_signed_data_posts_object(monkeypatch):
    from rts_pp_publish import append_signed_data, build_sign_requests

    captured: dict = {}

    class Resp:
        status_code = 200
        text = '[{"fileGuid":null,"signatureWithTimeStamp":"STAMPED"}]'

        def json(self):
            return [{"fileGuid": None, "signatureWithTimeStamp": "STAMPED"}]

    def fake_post(url, *, headers, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return Resp()

    monkeypatch.setattr("requests.post", fake_post)
    reqs = build_sign_requests({"stringForSign": "QQ==", "fileHashes": "[]"})
    out = append_signed_data(
        "c=1",
        [{"fileGuid": None, "signatureWithTimeStamp": "RAW"}],
        sign_requests=reqs,
    )
    assert "signatureWithSignedDatas" in captured["json"]
    assert isinstance(captured["json"], dict)
    assert captured["json"]["signatureWithSignedDatas"][0]["signedData"] == "QQ=="
    assert captured["json"]["signatureWithSignedDatas"][0]["signature"] == "RAW"
    assert out == [{"fileGuid": None, "signatureWithTimeStamp": "STAMPED"}]


def test_publish_job_db_roundtrip(tmp_path: Path):
    db = tmp_path / "a.sqlite3"
    jid = create_pp_publish_job(
        db,
        rts_draft_id=42,
        rts_profile="IPSokolova",
        source="monitor",
        source_key="oid1",
        status="awaiting_signature",
        prepare_json='{"stringForSign":"QQ=="}',
        sign_requests_json='[{"kind":"card"}]',
    )
    assert jid > 0
    job = get_pp_publish_job(db, jid)
    assert job is not None
    assert job["rts_draft_id"] == 42
    assert job["status"] == "awaiting_signature"
    assert job["prepare"]["stringForSign"] == "QQ=="
    found = find_pp_publish_job_for_draft(
        db, rts_draft_id=42, rts_profile="IPSokolova", statuses=["awaiting_signature"]
    )
    assert found is not None
    assert found["id"] == jid
    assert update_pp_publish_job(db, jid, status="published", published_at_utc="2026-01-01T00:00:00Z")
    job2 = get_pp_publish_job(db, jid)
    assert job2["status"] == "published"


def test_start_publish_disabled(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("RTS_PP_PUBLISH_ENABLED", raising=False)
    r = start_publish_job(
        analytics_db=tmp_path / "x.sqlite3",
        cookie_path=tmp_path / "c.txt",
        rts_draft_id=1,
    )
    assert r.ok is False
    assert r.reason == "disabled"
    assert r.http_status == 404


def test_start_publish_prepare_and_reuse(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("RTS_PP_PUBLISH_ENABLED", "1")
    db = tmp_path / "p.sqlite3"
    cookie = tmp_path / "cookies_rts.txt"
    cookie.write_text(".Auth.Sso=x; .Auth.Rts.P=y", encoding="utf-8")

    prepare_body = {
        "stringForSign": "PD94bWw+",
        "fileHashes": "[]",
        "offerPriceListItemId": 99,
    }

    with patch("rts_pp_publish_service.validate_cookie_session"), patch(
        "rts_pp_publish_service.prepare_for_publish", return_value=prepare_body
    ) as prep_mock:
        r1 = start_publish_job(
            analytics_db=db,
            cookie_path=cookie,
            rts_draft_id=99,
            rts_profile="IPSokolova",
            source="notice",
        )
        assert r1.ok
        assert r1.status == "awaiting_signature"
        assert r1.job_id
        assert len(r1.sign_requests) == 1
        prep_mock.assert_called_once()

        r2 = start_publish_job(
            analytics_db=db,
            cookie_path=cookie,
            rts_draft_id=99,
            rts_profile="IPSokolova",
        )
        assert r2.reused is True
        assert r2.job_id == r1.job_id
        assert prep_mock.call_count == 1


def test_submit_signatures_flow(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("RTS_PP_PUBLISH_ENABLED", "1")
    db = tmp_path / "p.sqlite3"
    cookie = tmp_path / "cookies_rts.txt"
    cookie.write_text(".Auth.Sso=x; .Auth.Rts.P=y", encoding="utf-8")
    prepare_body = {"stringForSign": "PD94bWw+", "fileHashes": "[]"}

    with patch("rts_pp_publish_service.validate_cookie_session"), patch(
        "rts_pp_publish_service.prepare_for_publish", return_value=prepare_body
    ):
        started = start_publish_job(
            analytics_db=db,
            cookie_path=cookie,
            rts_draft_id=77,
            rts_profile="Nacpro",
        )

    with patch("rts_pp_publish_service.validate_cookie_session"), patch(
        "rts_pp_publish_service.append_signed_data",
        return_value=[{"fileGuid": None, "signatureWithTimeStamp": "STAMPED"}],
    ) as tsa, patch(
        "rts_pp_publish_service.publish_offer", return_value={}
    ) as pub:
        done = submit_publish_signatures(
            analytics_db=db,
            cookie_path=cookie,
            job_id=started.job_id,
            signatures=[{"signatureWithTimeStamp": "RAWCMS"}],
            rts_profile="Nacpro",
        )
        assert done.ok
        assert done.status == "published"
        tsa.assert_called_once()
        _args, kwargs = tsa.call_args
        assert "sign_requests" in kwargs
        assert kwargs["sign_requests"]
        pub.assert_called_once()
        assert pub.call_args.kwargs.get("draft_id") == 77


def test_submit_signatures_notice_source_marks_attempt(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("RTS_PP_PUBLISH_ENABLED", "1")
    db = tmp_path / "p.sqlite3"
    cookie = tmp_path / "cookies_rts.txt"
    cookie.write_text(".Auth.Sso=x; .Auth.Rts.P=y", encoding="utf-8")
    prepare_body = {"stringForSign": "PD94bWw+", "fileHashes": "[]"}
    pn = "0369200012126000008"
    lk = f"{pn}|22.22.10.000-00000005|idx:0"

    with patch("rts_pp_publish_service.validate_cookie_session"), patch(
        "rts_pp_publish_service.prepare_for_publish", return_value=prepare_body
    ):
        started = start_publish_job(
            analytics_db=db,
            cookie_path=cookie,
            rts_draft_id=562287,
            rts_profile="IPSokolova",
            source="notice",
            source_key=f"{pn}|{lk}",
        )

    with patch("rts_pp_publish_service.validate_cookie_session"), patch(
        "rts_pp_publish_service.append_signed_data",
        return_value=[{"fileGuid": None, "signatureWithTimeStamp": "STAMPED"}],
    ), patch("rts_pp_publish_service.publish_offer", return_value={}):
        done = submit_publish_signatures(
            analytics_db=db,
            cookie_path=cookie,
            job_id=started.job_id,
            signatures=[{"signatureWithTimeStamp": "RAWCMS"}],
            rts_profile="IPSokolova",
        )
    assert done.ok
    assert done.status == "published"
    from eis_analytics_db import get_auto_participate_attempt

    attempt = get_auto_participate_attempt(db, pn, lk, rts_profile="IPSokolova")
    assert attempt is not None
    assert attempt["status"] == "published"
    assert int(attempt["rts_draft_id"]) == 562287


def test_list_pp_publish_jobs_includes_notice_source(tmp_path: Path):
    from eis_analytics_db import list_pp_publish_jobs

    db = tmp_path / "a.sqlite3"
    create_pp_publish_job(
        db,
        rts_draft_id=101,
        rts_profile="IPSokolova",
        source="notice",
        source_key="PN|lk",
        status="awaiting_signature",
    )
    create_pp_publish_job(
        db,
        rts_draft_id=102,
        rts_profile="IPSokolova",
        source="auto_monitor",
        source_key="oid",
        status="awaiting_signature",
    )
    notice = list_pp_publish_jobs(db, source="notice", rts_profile="IPSokolova")
    assert len(notice) == 1
    assert notice[0]["rts_draft_id"] == 101
    monitor = list_pp_publish_jobs(db, source="auto_monitor", rts_profile="IPSokolova")
    assert len(monitor) == 1
    all_jobs = list_pp_publish_jobs(db, source="", rts_profile="IPSokolova")
    assert {j["rts_draft_id"] for j in all_jobs} == {101, 102}
