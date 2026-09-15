# Справочник шаблонов ПП (Ktru/search dumps)

Положите сюда `draft_ktru_dict.txt` (как в prod: ответы dictionary Ktru/search).
Файл большой (~30 MB) и **не коммитится** в git.

Импорт в кэш приложения:

```bash
# локально / на VM из файла
python tools/import_draft_ktru_dict.py --cache-dir /var/lib/rts_local/ktru_rts_cache

# на co-hosted VM быстрее скопировать уже собранный кэш с prod
sudo bash deploy/sync_ktru_cache_from_prod.sh
```

Рабочий каталог кэша: `$RTS_META_DIR/ktru_rts_cache` (на rts-local: `/var/lib/rts_local/ktru_rts_cache`).
Без этих JSON оператор получит «КТРУ нет в справочнике шаблонов ПП (draft_ktru_dict)».
