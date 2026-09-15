<#
.SYNOPSIS
    Синхронизация вендорной копии client/ и lib/ из основного репозитория TrainingServerUI.

.DESCRIPTION
    По умолчанию скрипт только показывает различия (без изменений). С ключом
    -Apply перезаписывает скопированные файлы, а затем напоминает обновить VENDOR.md.

.EXAMPLE
    .\tools\sync_vendor.ps1
    .\tools\sync_vendor.ps1 -Apply
#>
[CmdletBinding()]
param(
    [string]$Source = 'D:\VSC Python projects\TrainingServerUI',
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'

$target = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath $Source)) {
    throw "Источник не найден: $Source"
}

$items = @(
    @{ From = Join-Path $Source 'client'; To = Join-Path $target 'client' },
    @{ From = Join-Path $Source 'lib';    To = Join-Path $target 'lib' },
    @{ From = Join-Path $Source 'ui\common\flash.py';      To = Join-Path $target 'acceptance\ui\common\flash.py' },
    @{ From = Join-Path $Source 'ui\common\pagination.py'; To = Join-Path $target 'acceptance\ui\common\pagination.py' }
)

function Get-Files([string]$Path) {
    if (Test-Path -LiteralPath $Path -PathType Leaf) { return @(Get-Item -LiteralPath $Path) }
    return @(Get-ChildItem -LiteralPath $Path -Recurse -File -Include *.py | Where-Object { $_.FullName -notmatch '__pycache__' })
}

function Get-Hash([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
}

$differences = @()

foreach ($item in $items) {
    foreach ($file in Get-Files $item.From) {
        $name = $file.Name
        $destinationFile = if (Test-Path -LiteralPath $item.To -PathType Leaf) {
            $item.To
        } else {
            Join-Path $item.To $name
        }

        if (-not (Test-Path -LiteralPath $destinationFile)) {
            $differences += "ТОЛЬКО В ИСТОЧНИКЕ : $($destinationFile.Replace($target, '.'))"
            continue
        }

        if ((Get-Hash $file.FullName) -ne (Get-Hash $destinationFile)) {
            $differences += "ОТЛИЧАЕТСЯ          : $($destinationFile.Replace($target, '.'))"
        }
    }
}

if ($differences.Count -eq 0) {
    Write-Host 'Копия совпадает с источником: расхождений нет.' -ForegroundColor Green
    exit 0
}

Write-Host 'Расхождения копии и источника:' -ForegroundColor Yellow
$differences | ForEach-Object { Write-Host "  $_" }

if (-not $Apply) {
    Write-Host ''
    Write-Host 'Режим проверки: файлы не изменялись. Запустите с -Apply, чтобы перезаписать копию.' -ForegroundColor Cyan
    exit 1
}

foreach ($item in $items) {
    if (Test-Path -LiteralPath $item.To -PathType Leaf) {
        Copy-Item -LiteralPath $item.From -Destination $item.To -Force
    } else {
        Copy-Item -LiteralPath $item.From -Destination $item.To -Recurse -Force
    }
}

$commit = (& git -C $Source rev-parse HEAD) 2>$null
Write-Host ''
Write-Host "Копия обновлена из $Source (commit $commit)." -ForegroundColor Green
Write-Host 'Обновите VENDOR.md: commit, дата и перечень локальных отличий от источника.' -ForegroundColor Yellow
Write-Host 'Проверьте, что аддитивные модули (client/tasks.py, client/datasets.py) на месте и тесты проходят.' -ForegroundColor Yellow
