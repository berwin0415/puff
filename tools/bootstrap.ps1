#!/usr/bin/env pwsh
# 首次本地初始化：安装固定工具链，再安装 workspace 依赖（Windows PowerShell）。
$ErrorActionPreference = 'Stop'

Set-Location (Resolve-Path (Join-Path $PSScriptRoot '..'))

if (Get-Command mise -ErrorAction SilentlyContinue) {
  mise install
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  mise exec -- pnpm install
} else {
  Write-Host 'mise not found; using the Node/pnpm on PATH.'
  pnpm install
}

if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Ready. Try 'pnpm -r --parallel run dev', 'pnpm exec nx graph' or 'node tools/checks/project-boundaries.mjs'."
