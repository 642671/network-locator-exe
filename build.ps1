param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Version = (Get-Content -LiteralPath (Join-Path $Root "VERSION") -Raw).Trim()
$DistDir = Join-Path $Root "dist"
$BuildDir = Join-Path $Root "build"
$ReleaseDir = Join-Path $Root "release"
$ExeName = "network-locator.exe"
$ExePath = Join-Path $ReleaseDir $ExeName

Set-Location -LiteralPath $Root

$PythonCommand = if ($env:NETLOC_PYTHON) { $env:NETLOC_PYTHON } else { "python" }
$PythonPrefix = @()
if ([IO.Path]::GetFileNameWithoutExtension($PythonCommand) -eq "py") {
    $PythonPrefix = @("-3")
}

& $PythonCommand @PythonPrefix tools\generate_icon.py
if ($LASTEXITCODE -ne 0) {
    throw "Icon generation failed"
}

if (-not $SkipTests) {
    & $PythonCommand @PythonPrefix -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) {
        throw "Python tests failed"
    }
    & $PythonCommand @PythonPrefix -m py_compile `
        launcher.py `
        src\network_locator\__init__.py `
        src\network_locator\core.py `
        src\network_locator\oui.py `
        src\network_locator\scanner.py `
        src\network_locator\server.py `
        src\network_locator\store.py `
        src\network_locator\win_network.py
    if ($LASTEXITCODE -ne 0) {
        throw "Python compilation failed"
    }
    if (Get-Command node -ErrorAction SilentlyContinue) {
        & node --check src\frontend\csv.js
        if ($LASTEXITCODE -ne 0) {
            throw "csv.js syntax check failed"
        }
        & node --check src\frontend\app.js
        if ($LASTEXITCODE -ne 0) {
            throw "app.js syntax check failed"
        }
        foreach ($script in @("v2-utils.js", "v2-api.js", "v2-app.js")) {
            & node --check (Join-Path $Root "src\frontend\$script")
            if ($LASTEXITCODE -ne 0) {
                throw "$script syntax check failed"
            }
        }
        & node --test tests\frontend_csv.test.js tests\frontend_search_state.test.js tests\frontend_saved_macs.test.js tests\frontend_v2_utils.test.js
        if ($LASTEXITCODE -ne 0) {
            throw "Frontend tests failed"
        }
    } else {
        Write-Warning "Node.js is unavailable; JavaScript checks were skipped."
    }
}

foreach ($path in @($DistDir, $BuildDir, $ReleaseDir)) {
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Recurse -Force
    }
}
New-Item -ItemType Directory -Path $DistDir, $BuildDir, $ReleaseDir -Force | Out-Null

& $PythonCommand @PythonPrefix -c "import webview, pathlib, sys; lib = pathlib.Path(webview.__file__).parent / 'lib'; print(lib); sys.exit(0 if lib.is_dir() else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "pywebview runtime libraries are missing; run pip install -r requirements-build.txt"
}

& $PythonCommand @PythonPrefix -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name network-locator `
    --icon (Join-Path $Root "assets\network-locator.ico") `
    --version-file (Join-Path $Root "version_info.txt") `
    --paths (Join-Path $Root "src") `
    --hidden-import webview.platforms.edgechromium `
    --add-data "$(Join-Path $Root 'src\frontend');frontend" `
    --add-data "$(Join-Path $Root 'assets\network-locator.ico');assets" `
    --distpath $DistDir `
    --workpath $BuildDir `
    --specpath $BuildDir `
    launcher.py
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed"
}

$BuiltExe = Join-Path $DistDir "network-locator.exe"
if (-not (Test-Path -LiteralPath $BuiltExe)) {
    throw "Built executable was not found"
}
Copy-Item -LiteralPath $BuiltExe -Destination $ExePath -Force

$SelfTestResult = Join-Path $ReleaseDir "selftest.json"
# Allow the CI runner's large subnet to be reported by the self-test only.
$PreviousMaxHosts = $env:SCAN_MAX_HOSTS
$env:SCAN_MAX_HOSTS = "65534"
try {
    $Process = Start-Process `
        -FilePath $ExePath `
        -ArgumentList @("--selftest", "--result-file", "`"$SelfTestResult`"") `
        -Wait `
        -PassThru
} finally {
    if ($null -eq $PreviousMaxHosts) {
        Remove-Item Env:SCAN_MAX_HOSTS -ErrorAction SilentlyContinue
    } else {
        $env:SCAN_MAX_HOSTS = $PreviousMaxHosts
    }
}
if ($Process.ExitCode -ne 0) {
    throw "EXE self-test failed with exit code $($Process.ExitCode)"
}
$SelfTest = Get-Content -LiteralPath $SelfTestResult -Raw -Encoding UTF8 | ConvertFrom-Json
if ($SelfTest.status -ne "ok") {
    throw "EXE self-test returned status $($SelfTest.status)"
}
foreach ($key in @("index_html", "styles_v2_css", "v2_utils_js", "v2_api_js", "v2_app_js")) {
    if (-not $SelfTest.frontend.$key) {
        throw "EXE self-test is missing bundled WebUI file: $key"
    }
}
if (-not $SelfTest.webview_module) {
    throw "EXE self-test did not find the bundled pywebview module"
}
if (-not $SelfTest.icon) {
    throw "EXE self-test did not find the bundled application icon"
}

$ReadyFile = Join-Path $ReleaseDir "ready.json"
$Smoke = Start-Process `
    -FilePath $ExePath `
    -ArgumentList @("--serve-only", "--ready-file", "`"$ReadyFile`"", "--port", "0", "--duration", "6") `
    -PassThru
$deadline = (Get-Date).AddSeconds(30)
while (-not (Test-Path -LiteralPath $ReadyFile) -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 200
}
if (-not (Test-Path -LiteralPath $ReadyFile)) {
    throw "serve-only did not report a ready file"
}
$Ready = Get-Content -LiteralPath $ReadyFile -Raw -Encoding UTF8 | ConvertFrom-Json
$Health = Invoke-RestMethod -Uri ($Ready.url.TrimEnd("/") + "/health") -TimeoutSec 10
if ($Health.app -ne "network-locator" -or $Health.version -ne $Version) {
    throw "health payload mismatch: $($Health | ConvertTo-Json -Compress)"
}
$HomeResponse = Invoke-WebRequest -Uri $Ready.url -TimeoutSec 10 -UseBasicParsing
if ($HomeResponse.StatusCode -ne 200 -or $HomeResponse.Content -notmatch "网络定位") {
    throw "WebUI index did not render"
}
if ($HomeResponse.Content -match "__APP_VERSION__") {
    throw "WebUI version placeholder was not substituted"
}
if (-not $Smoke.WaitForExit(20000)) {
    $Smoke.Kill()
    throw "serve-only process did not exit after its duration"
}
if ($Smoke.ExitCode -ne 0) {
    throw "serve-only process exited with $($Smoke.ExitCode)"
}

$ExeHash = (Get-FileHash -LiteralPath $ExePath -Algorithm SHA256).Hash.ToLowerInvariant()
"$ExeHash  $ExeName" | Set-Content -LiteralPath (Join-Path $ReleaseDir "$ExeName.sha256") -Encoding ascii

if ($SelfTest.warnings) {
    Write-Output ("SelfTestWarnings : " + ($SelfTest.warnings -join " | "))
}
[pscustomobject]@{
    Version = $Version
    Exe = $ExePath
    ExeBytes = (Get-Item -LiteralPath $ExePath).Length
    ExeSha256 = $ExeHash
    SelfTestStatus = $SelfTest.status
    SelfTestNetworks = $SelfTest.network_count
    WebUiIndex = $SelfTest.frontend.index_html
    WebView2Runtime = $SelfTest.webview2_runtime
    SmokeHealth = $Health.status
} | Format-List
