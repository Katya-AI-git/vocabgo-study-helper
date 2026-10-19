$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$installDir = Join-Path $root "runtime\python"
$pythonExe = Join-Path $installDir "python.exe"
if (Test-Path -LiteralPath $pythonExe) {
    exit 0
}

$version = "3.12.10"
$arch = if ([Environment]::Is64BitOperatingSystem) { "amd64" } else { "win32" }
$installerName = if ($arch -eq "amd64") {
    "python-$version-amd64.exe"
} else {
    "python-$version.exe"
}
$url = "https://www.python.org/ftp/python/$version/$installerName"
$installer = Join-Path $env:TEMP $installerName

Write-Host "[*] 下载官方 Python：$url"
$ProgressPreference = "SilentlyContinue"
Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $installer -TimeoutSec 180

$signature = Get-AuthenticodeSignature -FilePath $installer
if ($signature.Status -ne "Valid" -or $signature.SignerCertificate.Subject -notmatch "Python Software Foundation") {
    throw "Python 安装包数字签名校验失败，已停止安装"
}

$installArgs = @(
    "/quiet",
    "InstallAllUsers=0",
    "Include_launcher=0",
    "Include_test=0",
    "Include_doc=0",
    "Include_tcltk=1",
    "Include_pip=1",
    "PrependPath=0",
    "TargetDir=$installDir"
)
$process = Start-Process -FilePath $installer -ArgumentList $installArgs -Wait -PassThru -WindowStyle Hidden
if ($process.ExitCode -ne 0) {
    throw "Python 安装失败，退出码：$($process.ExitCode)"
}
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "Python 安装结束后未找到 $pythonExe"
}

Write-Host "[*] 官方 Python 已安装到项目 runtime 目录"
