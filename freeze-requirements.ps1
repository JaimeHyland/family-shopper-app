$requirements = & python -m pip freeze

if ($LASTEXITCODE -ne 0) {
    throw "pip freeze failed. requirements.txt was not updated."
}

[System.IO.File]::WriteAllLines(
    (Join-Path $PSScriptRoot "requirements.txt"),
    $requirements,
    [System.Text.UTF8Encoding]::new($false)
)

Write-Host "requirements.txt updated successfully (UTF-8, no BOM)."
