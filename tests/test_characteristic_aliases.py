from characteristic_aliases import label_alias_keys, labels_are_aliases


def test_label_alias_keys_display_pair():
    keys = label_alias_keys("Наличие дисплея")
    assert "наличие дисплея" in keys
    assert "наличие жк-дисплея" in keys


def test_labels_are_aliases():
    assert labels_are_aliases("Наличие дисплея", "Наличие ЖК-дисплея")
    assert not labels_are_aliases("Наличие дисплея", "Наличие факса")
