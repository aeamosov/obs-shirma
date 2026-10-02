# Run with powershell -NoProfile -File tests/install-finalization.ps1 (also supports pwsh).
# Exercise the actual installer finalization without installing or launching OBS.
$ErrorActionPreference = "Stop"
$tokens = $null
$parseErrors = $null
$installer = Join-Path (Split-Path $PSScriptRoot) "install.ps1"
$ast = [Management.Automation.Language.Parser]::ParseFile($installer, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors) { throw ($parseErrors | Out-String) }
$refreshFunction = $ast.Find({ param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq "Refresh-ShortcutIcons"
}, $true)
if (-not $refreshFunction) { throw "Missing icon refresh function" }
. ([scriptblock]::Create($refreshFunction.Extent.Text))
$refreshCall = $ast.Find({ param($node)
    $node -is [Management.Automation.Language.CommandAst] -and
    $node.GetCommandName() -eq "Refresh-ShortcutIcons"
}, $true)
if (-not $refreshCall) { throw "Installer does not call icon refresh" }
$tail = [scriptblock]::Create($ast.Extent.Text.Substring($refreshCall.Extent.StartOffset))

# Mocks are scoped to this test script. No real system files or processes are changed.
function Test-Path {
    param($LiteralPath, $PathType)
    if ($LiteralPath -ne $script:refreshPath -or $PathType -ne "Leaf") {
        throw "Unexpected icon utility lookup"
    }
    return $script:utilityExists
}
function Start-Process {
    param($FilePath, $WorkingDirectory, $WindowStyle, $ArgumentList)
    if ($FilePath -ne $script:Obs -or "--startvirtualcam" -notin $ArgumentList -or $WindowStyle -ne "Hidden") {
        throw "Incorrect Shirma startup"
    }
    $script:started = $true
}
function Remove-Item { param($Path, [switch]$Force, $ErrorAction) }
function Step { param($msg) }
function L { param($en, $ru) return $en }
function Write-Host { param($Object, $ForegroundColor) }

$originalRoot = $env:SystemRoot
try {
    $env:SystemRoot = "C:\shirma-test-windows"
    $script:refreshPath = Join-Path $env:SystemRoot "System32\ie4uinit.exe"
    $script:Obs = "C:\shirma-test-obs\obs64.exe"
    $Data = "C:\shirma-test-data"
    foreach ($case in "missing", "success", "nonzero", "blocked") {
        $script:utilityExists = $case -ne "missing"
        $script:started = $false
        $script:called = $false
        $script:correctArgs = $false
        $script:case = $case
        Set-Item -Path ("Function:" + $script:refreshPath) -Value {
            $script:called = $true
            $script:correctArgs = $args.Count -eq 1 -and $args[0] -eq "-show"
            if ($script:case -eq "blocked") { throw "Access denied to icon utility" }
            $global:LASTEXITCODE = $(if ($script:case -eq "nonzero") { 1 } else { 0 })
        }
        & $tail
        if (-not $script:started) { throw "$case : installer did not reach first startup" }
        if ($script:called -ne $script:utilityExists) { throw "$case : unexpected utility invocation" }
        if ($script:utilityExists -and -not $script:correctArgs) { throw "$case : incorrect refresh arguments" }
        [Console]::WriteLine("PASS: {0} icon refresh still completes installation", $case)
    }
} finally {
    Microsoft.PowerShell.Management\Remove-Item -LiteralPath ("Function:" + $script:refreshPath) -ErrorAction SilentlyContinue
    $env:SystemRoot = $originalRoot
}
