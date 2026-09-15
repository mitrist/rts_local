"""Разбор блока KTRU/characteristics (EP sample)."""

from __future__ import annotations

import pytest

from eis_changelog import enrich_changelog_rows_from_customer_characteristics
from eis_ktru_characteristics import extract_ktru_customer_lines_from_xml_bytes


@pytest.fixture()
def ez_sample_xml_bytes() -> bytes:
    xml = """\
  <notificationInfo xmlns="http://zakupki.gov.ru/fcs/schemas/notification">
    <customerRequirementsInfo>
      <customerRequirementInfo>
        <customer>
          <regNum>03492000123</regNum>
          <fullName>ООО «Тестовый заказчик»</fullName>
          <inn>7707123456</inn>
        </customer>
        <contractConditionsInfo>
          <maxPriceInfo>
            <maxPrice>100</maxPrice>
            <currency><code>RUB</code></currency>
          </maxPriceInfo>
          <deliveryPlacesInfo>
            <deliveryPlaceInfo>
              <deliveryPlace>г. Тверь, ул. Примерная, д. 1</deliveryPlace>
              <kladr>
                <kladrCode>6900000100000</kladrCode>
                <fullName>г Тверь</fullName>
              </kladr>
            </deliveryPlaceInfo>
          </deliveryPlacesInfo>
          <deliveryTerm>30 календарных дней</deliveryTerm>
        </contractConditionsInfo>
      </customerRequirementInfo>
    </customerRequirementsInfo>
    <purchaseObjectsInfo>
      <purchaseObject>
        <sid>1001</sid>
        <KTRU>
          <code>32.97.11.160-00000001</code>
          <characteristics>
            <characteristicsUsingReferenceInfo>
              <code>CHR_REF_1</code>
              <name>Масса</name>
              <type>2</type>
              <kind>2</kind>
              <values>
                <value>
                  <qualityDescription>нетто 1 кг</qualityDescription>
                </value>
              </values>
            </characteristicsUsingReferenceInfo>
            <characteristicsUsingTextForm>
              <name>Цвет корпуса</name>
              <type>1</type>
              <values>
                <value>
                  <qualityDescription>синий / белый</qualityDescription>
                </value>
              </values>
            </characteristicsUsingTextForm>
          </characteristics>
        </KTRU>
      </purchaseObject>
    </purchaseObjectsInfo>
  </notificationInfo>
""".strip()
    return xml.encode("utf-8")


def test_extract_ktru_lines_reference_and_manual(ez_sample_xml_bytes: bytes):
    rows = extract_ktru_customer_lines_from_xml_bytes(ez_sample_xml_bytes, purchase_number="0373200008325000008")
    assert len(rows) == 1
    ln = rows[0]
    assert ln.ktru_code == "32.97.11.160-00000001"
    assert ln.object_sid == "1001"
    assert len(ln.characteristics) == 2
    kinds = {c.characteristic_kind for c in ln.characteristics}
    assert kinds == {"reference", "manual"}
    refs = [c for c in ln.characteristics if c.characteristic_kind == "reference"]
    assert refs
    assert refs[0].characteristic_code == "CHR_REF_1"
    assert refs[0].display_label.lower().startswith("масса")

    manual = [c for c in ln.characteristics if c.characteristic_kind == "manual"]
    assert manual and "цвет" in manual[0].display_label.lower()

    co = ln.customer_organization_display
    assert "Тестовый заказчик" in co
    assert "ИНН 7707123456" in co
    assert "Адрес поставки" in co
    assert "Тверь" in co


def test_extract_drug_purchase_object_lines():
    xml = """\
  <notificationInfo xmlns="http://zakupki.gov.ru/fcs/schemas/notification">
    <customerRequirementsInfo>
      <customerRequirementInfo>
        <customer>
          <regNum>03303000112</regNum>
          <fullName>БУЗ «Тестовая больница»</fullName>
          <inn>3525000000</inn>
        </customer>
        <contractConditionsInfo>
          <maxPriceInfo>
            <maxPrice>1200</maxPrice>
            <currency><code>RUB</code></currency>
          </maxPriceInfo>
          <deliveryPlacesInfo>
            <deliveryPlaceInfo><deliveryPlace>г. Вологда</deliveryPlace></deliveryPlaceInfo>
          </deliveryPlacesInfo>
        </contractConditionsInfo>
      </customerRequirementInfo>
    </customerRequirementsInfo>
    <purchaseObjectsInfo>
      <drugPurchaseObjectsInfo>
        <drugPurchaseObjectInfo>
          <sid>2001</sid>
          <objectInfoUsingReferenceInfo>
            <drugsInfo>
              <drugInfo>
                <MNNInfo>
                  <MNNExternalCode>mnn-1</MNNExternalCode>
                  <MNNName>Парацетамол</MNNName>
                </MNNInfo>
                <KTRU><code>21.20.10.234-00004</code></KTRU>
                <medicamentalFormInfo><medicamentalFormName>таблетки</medicamentalFormName></medicamentalFormInfo>
                <dosageInfo><dosageGRLSValue>500 мг</dosageGRLSValue></dosageInfo>
                <packagingInfo><sumaryPackagingQuantity>20</sumaryPackagingQuantity></packagingInfo>
                <drugQuantity>10</drugQuantity>
              </drugInfo>
            </drugsInfo>
          </objectInfoUsingReferenceInfo>
          <name>Парацетамол таблетки</name>
          <isZNVLP>true</isZNVLP>
          <drugQuantityCustomersInfo>
            <drugQuantityCustomerInfo>
              <customer><regNum>03303000112</regNum></customer>
              <quantity>10</quantity>
            </drugQuantityCustomerInfo>
          </drugQuantityCustomersInfo>
          <pricePerUnit>12.50</pricePerUnit>
          <positionPrice>125.00</positionPrice>
        </drugPurchaseObjectInfo>
      </drugPurchaseObjectsInfo>
    </purchaseObjectsInfo>
  </notificationInfo>
""".strip()
    rows = extract_ktru_customer_lines_from_xml_bytes(xml.encode("utf-8"), purchase_number="0330300011226000160")

    assert len(rows) == 1
    ln = rows[0]
    assert ln.ktru_code == "21.20.10.234-00004"
    assert ln.object_sid == "2001"
    assert ln.position_display_name == "Парацетамол таблетки"
    assert ln.price_per_unit_display == "12.50"
    assert ln.line_sum_display == "125.00"
    assert "Тестовая больница" in ln.customer_organization_display
    assert "Вологда" in ln.customer_organization_display
    labels = {c.display_label: c.value_display for c in ln.characteristics}
    assert labels["МНН"] == "Парацетамол"
    assert labels["Лекарственная форма"] == "таблетки"
    assert labels["Дозировка"] == "500 мг"
    assert labels["ЖНВЛП"] == "true"


def test_enrich_changelog_from_last_eis_prefers_line_key():
    rows: list[dict] = [
        {
            "purchase_number": "pn-wrong",
            "ktru": "K1",
            "customer_line_key": "lk-special",
        }
    ]
    last_eis = {
        "customer_characteristics": [
            {
                "line_key": "lk-special",
                "purchase_number": "PN-OK",
                "ktru_code": "K1",
                "characteristics": [{"a": 1}],
                "notice_total_display": "500 RUB",
            }
        ]
    }
    enrich_changelog_rows_from_customer_characteristics(rows, last_eis)
    assert rows[0]["characteristics_param_count"] == 1
    assert rows[0]["notice_total_display"] == "500 RUB"


def test_enrich_changelog_falls_back_pair_and_skips_when_present():
    rows: list[dict] = [{"purchase_number": "X", "ktru": "Y", "characteristics_param_count": 9, "notice_total_display": ""}]
    last_eis = {
        "customer_characteristics": [
            {"purchase_number": "X", "ktru_code": "Y", "characteristics": [{}], "notice_total_display": "1 RUB"},
        ]
    }
    enrich_changelog_rows_from_customer_characteristics(rows, last_eis)
    assert rows[0]["characteristics_param_count"] == 9
    assert rows[0]["notice_total_display"] == "1 RUB"
