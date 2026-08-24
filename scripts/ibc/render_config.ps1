# Helper for start_gateway.bat: renders config.ini.template into IBC's
# directory with ${IB_USERNAME}/${IB_PASSWORD}/${IB_PORT} filled in from the
# current process's environment (which start_gateway.bat has already loaded
# from .env). Kept as its own file rather than an inline -Command one-liner
# because quoting a multi-substitution pipeline through batch's already-odd
# quoting rules is a reliable source of silently-wrong output.
param(
    [Parameter(Mandatory = $true)][string]$TemplatePath,
    [Parameter(Mandatory = $true)][string]$OutputPath
)

$content = Get-Content -Raw $TemplatePath
$content = $content.Replace('${IB_USERNAME}', $env:IB_USERNAME)
$content = $content.Replace('${IB_PASSWORD}', $env:IB_PASSWORD)
$content = $content.Replace('${IB_PORT}', $env:IB_PORT)
Set-Content -NoNewline -Path $OutputPath -Value $content
