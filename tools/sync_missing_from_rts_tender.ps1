# Синхронизация недостающих модулей из rts_tender → rts_local
#
# Запуск (PowerShell):
#   cd E:\D\01_Projects\rts_local
#   powershell -ExecutionPolicy Bypass -File tools\sync_missing_from_rts_tender.ps1
#
# По умолчанию источник: ..\rts_tender (соседняя папка).
# Переопределение:
#   $env:RTS_TENDER_SRC = "E:\D\01_Projects\rts_tender"

param(
    [string]$Source = $env:RTS_TENDER_SRC,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $Source) {
    $Source = Join-Path (Split-Path -Parent $Root) "rts_tender"
}
if (-not (Test-Path $Source)) {
    Write-Error "Источник не найден: $Source. Укажите -Source или RTS_TENDER_SRC."
}

Write-Host "Source: $Source"
Write-Host "Target: $Root"

$modules = @(
    "auto_participate.py",
    "auto_participate_settings.py",
    "eis_notice_event_journal_fetch.py",
    "eis_notice_event_journal_parse.py",
    "eis_offer_matching_outcome.py",
    "eis_org_regions.py",
    "eis_history_backfill.py",
    "eis_pp_event_chain.py",
    "eis_pp_link.py",
    "eis_pp_link_service.py",
    "eis_pp_manual_register.py",
    "eis_pp_scenario_tags.py",
    "eis_supplier_results_fetch.py",
    "eis_supplier_results_parse.py",
    "eis_winner_price_key.py",
    "pp_prehistory.py",
    "rts_lot_items.py",
    "rts_registry_import.py"
)

$copied = 0
$skipped = 0
$missingSrc = @()

foreach ($m in $modules) {
    $src = Join-Path $Source $m
    $dst = Join-Path $Root $m
    if (-not (Test-Path $src)) {
        $missingSrc += $m
        continue
    }
    if ((Test-Path $dst) -and -not $Force) {
        Write-Host "skip  $m (already exists; use -Force to overwrite)"
        $skipped++
        continue
    }
    Copy-Item $src $dst -Force
    Write-Host "copy  $m"
    $copied++
}

$tmplSrc = Join-Path $Source "web_ui\templates"
$tmplDst = Join-Path $Root "web_ui\templates"
if (Test-Path $tmplSrc) {
    $srcNames = Get-ChildItem $tmplSrc -File | ForEach-Object Name
    $dstNames = @{}
    if (Test-Path $tmplDst) {
        Get-ChildItem $tmplDst -File | ForEach-Object { $dstNames[$_.Name] = $true }
    } else {
        New-Item -ItemType Directory -Force -Path $tmplDst | Out-Null
    }
    foreach ($name in $srcNames) {
        if ($dstNames.ContainsKey($name) -and -not $Force) {
            continue
        }
        Copy-Item (Join-Path $tmplSrc $name) (Join-Path $tmplDst $name) -Force
        Write-Host "copy  web_ui/templates/$name"
        $copied++
    }
}

$extraTools = @(
    "import_ktru_csv.py",
    "rts_tender_watchdog.py",
    "history_job_report.py",
    "backfill_eis_pp_link.py"
)
foreach ($t in $extraTools) {
    $src = Join-Path $Source "tools\$t"
    $dst = Join-Path $Root "tools\$t"
    if (-not (Test-Path $src)) { continue }
    if ((Test-Path $dst) -and -not $Force) { continue }
    Copy-Item $src $dst -Force
    Write-Host "copy  tools/$t"
    $copied++
}

Write-Host ""
Write-Host "Done. copied=$copied skipped=$skipped"
if ($missingSrc.Count) {
    Write-Host "Not found in source:"
    $missingSrc | ForEach-Object { Write-Host "  - $_" }
}

Write-Host ""
Write-Host "Next: python -c `"import web_ui.main`""
