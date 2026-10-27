$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'

if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
    Write-Host 'Created .env. Set your API key, model and endpoint, then run this launcher again.'
    Write-Host 'See SELF_HOSTING.md for configuration instructions.'
    exit 0
}

function Test-SupportedCustomerPython {
    param(
        [string]$Executable,
        [string[]]$PrefixArguments = @()
    )
    try {
        $null = & $Executable @PrefixArguments -c 'import sys; sys.exit(0 if sys.version_info[:2] in ((3, 12), (3, 13)) else 1)' 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

$customerPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath '.venv') {
    if (-not (Test-Path -LiteralPath $customerPython -PathType Leaf)) {
        throw 'The existing .venv is incomplete. Move it aside manually, then run this launcher again. No files were removed.'
    }
    if (-not (Test-SupportedCustomerPython -Executable $customerPython)) {
        throw 'The existing .venv must use Python 3.12 or 3.13. Python 3.14 and other versions are not supported. Move .venv aside manually to create a supported environment; no files were removed.'
    }
} else {
    $customerBasePython = $null
    $customerPythonArguments = @()
    $customerLauncher = Get-Command py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($customerLauncher) {
        foreach ($customerVersion in @('3.13', '3.12')) {
            if (Test-SupportedCustomerPython -Executable $customerLauncher.Source -PrefixArguments @("-$customerVersion")) {
                $customerBasePython = $customerLauncher.Source
                $customerPythonArguments = @("-$customerVersion")
                break
            }
        }
    }
    if (-not $customerBasePython) {
        $customerFallback = Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($customerFallback -and (Test-SupportedCustomerPython -Executable $customerFallback.Source)) {
            $customerBasePython = $customerFallback.Source
        }
    }
    if (-not $customerBasePython) {
        throw 'Install Python 3.12 or 3.13 and run this launcher again. Python 3.14 and other versions are not supported by the pinned dependencies.'
    }
    & $customerBasePython @customerPythonArguments -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create .venv. Check your Python 3.12/3.13 installation and folder permissions.' }
    if (-not (Test-SupportedCustomerPython -Executable $customerPython)) {
        throw 'The new environment did not pass the Python 3.12/3.13 check. Check your installation; the environment was left in place.'
    }
}

& $customerPython -m ensurepip --upgrade
if ($LASTEXITCODE -ne 0) { throw 'Could not initialize pip in .venv.' }
& $customerPython -m pip install --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check your network connection and try again.' }
& $customerPython -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false
exit $LASTEXITCODE
