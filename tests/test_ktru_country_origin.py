from __future__ import annotations

from pathlib import Path

import pytest

from ktru_country_origin import (
    CountryOfOrigin,
    apply_country_to_draft_item,
    delete_country_origin_row,
    load_country_origin_rows,
    national_mode_groups_for_country,
    resolve_country_for_draft,
    save_country_origin_rows,
    upsert_country_origin_row,
    CountryOriginRow,
)


def test_load_legacy_two_column_csv(tmp_path: Path):
    path = tmp_path / "country_origin.csv"
    path.write_text(
        "position_display_name;Столбец1\n"
        "3D-принтер FDM;Китайская Народная Республика\n"
        "Диван;Российская Федерация\n",
        encoding="utf-8",
    )
    rows = load_country_origin_rows(path)
    assert len(rows) == 2
    assert rows[0].title == "3D-принтер FDM"
    assert rows[0].country_name == "Китайская Народная Республика"
    assert rows[0].enabled is True
    assert rows[0].ktru_code == ""


def test_save_and_reload_full_format(tmp_path: Path):
    path = tmp_path / "country_origin.csv"
    save_country_origin_rows(
        path,
        [
            CountryOriginRow(
                row_id="abc123",
                ktru_code="26.20.16.120-00000104",
                title="3D-принтер FDM",
                country_name="Китайская Народная Республика",
                enabled=True,
            )
        ],
    )
    rows = load_country_origin_rows(path)
    assert len(rows) == 1
    assert rows[0].row_id == "abc123"
    assert rows[0].ktru_code == "26.20.16.120-00000104"


def test_upsert_and_delete(tmp_path: Path):
    path = tmp_path / "country_origin.csv"
    saved = upsert_country_origin_row(
        path,
        title="Диван",
        country_name="Российская Федерация",
        enabled=True,
    )
    rows = load_country_origin_rows(path)
    assert len(rows) == 1
    upsert_country_origin_row(
        path,
        row_id=saved.row_id,
        title="Диван",
        country_name="Российская Федерация",
        ktru_code="31.09.12.110-00000004",
        enabled=False,
    )
    rows = load_country_origin_rows(path)
    assert rows[0].ktru_code == "31.09.12.110-00000004"
    assert rows[0].enabled is False
    assert delete_country_origin_row(path, saved.row_id)
    assert load_country_origin_rows(path) == []


def test_resolve_by_ktru_then_title():
    rows = [
        CountryOriginRow(
            row_id="1",
            ktru_code="",
            title="3D-принтер FDM",
            country_name="Китайская Народная Республика",
            enabled=True,
        ),
        CountryOriginRow(
            row_id="2",
            ktru_code="26.20.16.110-00000002",
            title="Клавиатура",
            country_name="Российская Федерация",
            enabled=True,
        ),
        CountryOriginRow(
            row_id="3",
            ktru_code="26.20.16.110-00000003",
            title="Клавиатура",
            country_name="Китайская Народная Республика",
            enabled=True,
        ),
        CountryOriginRow(
            row_id="4",
            ktru_code="",
            title="Выключено",
            country_name="Китайская Народная Республика",
            enabled=False,
        ),
    ]
    china = resolve_country_for_draft(title="3D-принтер FDM", rows=rows)
    assert china == CountryOfOrigin("156", "Китайская Народная Республика")
    ru_kb = resolve_country_for_draft(ktru_code="26.20.16.110-00000002", title="Клавиатура", rows=rows)
    assert ru_kb.code == "643"
    cn_kb = resolve_country_for_draft(ktru_code="26.20.16.110-00000003", title="Клавиатура", rows=rows)
    assert cn_kb.code == "156"
    default = resolve_country_for_draft(title="Неизвестный товар", rows=rows)
    assert default.code == "643"


def test_unknown_country_on_save(tmp_path: Path):
    path = tmp_path / "country_origin.csv"
    with pytest.raises(ValueError, match="Неизвестная страна"):
        save_country_origin_rows(
            path,
            [
                CountryOriginRow(
                    row_id="x",
                    ktru_code="",
                    title="Test",
                    country_name="Германия",
                    enabled=True,
                )
            ],
        )


def test_national_mode_groups_rf_vs_china():
    rf = CountryOfOrigin("643", "Российская Федерация")
    cn = CountryOfOrigin("156", "Китайская Народная Республика")
    rf_national, rf_software = national_mode_groups_for_country(rf)
    cn_national, cn_software = national_mode_groups_for_country(cn)
    assert rf_national[0]["nationalModeInfoTypeId"] == "IndustrialProductsRussianFederation"
    assert rf_software[0]["nationalModeInfoTypeId"] == "SoftwareRussianFederation"
    assert cn_national == []
    assert cn_software == []


def test_apply_country_to_draft_item_china():
    item: dict = {
        "countriesOfOrigin": [{"code": "643", "name": "Российская Федерация"}],
        "nationalModeInfoGroups": [
            {
                "countryCode": "643",
                "nationalModeInfoTypeId": "IndustrialProductsRussianFederation",
                "nationalModeInfos": [{"registryNumberNotProvided": True}],
            }
        ],
        "softwareNationalModeInfoGroups": [
            {
                "countryCode": "643",
                "nationalModeInfoTypeId": "SoftwareRussianFederation",
                "nationalModeInfos": [{"registryNumberNotProvided": True}],
            }
        ],
    }
    apply_country_to_draft_item(item, CountryOfOrigin("156", "Китайская Народная Республика"))
    assert item["countriesOfOrigin"] == [{"code": "156", "name": "Китайская Народная Республика"}]
    assert item["nationalModeInfoGroups"] == []
    assert item["softwareNationalModeInfoGroups"] == []
