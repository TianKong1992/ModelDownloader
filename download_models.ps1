<#
.SYNOPSIS
    使用 aria2c 下载模型文件（Windows 版本）
.DESCRIPTION
    从内嵌 JSON 或外部文件读取模型列表，依次下载。
    支持 SHA256 校验、断点续传、HF 镜像加速、独立 baseDir。
.PARAMETER BaseDir
    模型存放的根目录，默认为当前目录（模型 path 字段以此为根）
.PARAMETER HfMirror
    Hugging Face 镜像地址，内置默认 hf-mirror.com
.PARAMETER HfToken
    Hugging Face Token（如需下载受限模型）
.PARAMETER ModelFile
    外部 JSON 文件路径，优先级高于内嵌 MODEL_LIST_JSON
.EXAMPLE
    .\download_models.ps1
    .\download_models.ps1 -BaseDir "D:\ComfyUI" -HfMirror "https://hf-mirror.com"
    .\download_models.ps1 -ModelFile "temp\download_abc.json"
#>

param(
    [string]$BaseDir = ".",
    [string]$HfMirror = "https://hf-mirror.com",
    [string]$HfToken = "",
    [string]$ModelFile = ""
)

# ================= 强制实时刷新输出 =================
function Write-Log {
    param([string]$Message)
    Microsoft.PowerShell.Utility\Write-Output $Message
    try { [Console]::Out.Flush() } catch { }
}

# ================= 内嵌 JSON（仅当未提供 ModelFile 时使用） =================
$MODEL_LIST_JSON = @'
[
   {
    "filename": "z_image_base_bf16.safetensors",
    "path": "models/diffusion_models/z_image_base_bf16.safetensors",
    "repo": "cnb_recursive",
    "sha256": "ddb9098fd950631708db0b7248932cc42f007b36c34a141f6ffedbd64aec9a72",
    "size": "11.46 GiB",
    "source": "cnb.cool",
    "url": "https://cnb.cool/ai-models/huyuefeitool/z_image_base/-/lfs/ddb9098fd950631708db0b7248932cc42f007b36c34a141f6ffedbd64aec9a72?name=z_image_base_bf16.safetensors",
  }
]
'@

# ================= 前置检查 =================
# 从脚本同目录找 aria2c.exe（打包后自动提取到此）
$scriptDir = Split-Path -Parent $PSCommandPath
$ARIA2_EXE = Join-Path $scriptDir "aria2c.exe"
if (-not (Test-Path $ARIA2_EXE)) {
    Write-Log "[ERROR] 未找到 aria2c.exe，请将其放到工具所在目录。"
    Write-Log "       下载地址: https://github.com/aria2/aria2/releases"
    exit 1
}

# ================= 解析模型列表 =================
Write-Log "==================================================="
Write-Log "  解析模型列表..."
Write-Log "==================================================="

if ($ModelFile -and (Test-Path $ModelFile)) {
    Write-Log "  从文件读取: $ModelFile"
    try {
        $jsonContent = Get-Content -Path $ModelFile -Raw -Encoding UTF8
        $modelList = $jsonContent | ConvertFrom-Json
    } catch {
        Write-Log "[ERROR] 模型文件 $ModelFile 解析失败: $_"
        exit 1
    }
} else {
    try {
        $modelList = $MODEL_LIST_JSON | ConvertFrom-Json
    } catch {
        Write-Log "[ERROR] JSON 解析失败: $_"
        exit 1
    }
}

Write-Log "  共 $($modelList.Count) 个模型文件"

# ================= 初始化 =================
$BaseDir = (Resolve-Path $BaseDir).Path
$SUCCESS = 0
$FAIL    = 0
$SKIP    = 0
$TOTAL   = $modelList.Count
$INDEX   = 0

# ================= 下载循环 =================
foreach ($item in $modelList) {
    $INDEX++
    $url      = $item.url
    $relPath  = $item.path
    $sha256   = if ($item.sha256)  { $item.sha256.Trim()  } else { "" }
    $size     = if ($item.size)    { $item.size           } else { "unknown" }
    $filename = if ($item.filename){ $item.filename        } else { Split-Path -Leaf $relPath }

    if (-not $url -or -not $relPath) { continue }

    # --- 1. 路径构建 ---
    $modelBaseDir = if ($item.baseDir) { $item.baseDir } else { $BaseDir }
    try { $modelBaseDir = (Resolve-Path $modelBaseDir -ErrorAction Stop).Path } catch { }
    $fullPath = Join-Path $modelBaseDir $relPath
    $saveDir  = Split-Path $fullPath -Parent
    $fileName = Split-Path $fullPath -Leaf

    if (-not (Test-Path $saveDir)) {
        New-Item -ItemType Directory -Path $saveDir -Force | Out-Null
        Write-Log "[MKDIR] $saveDir"
    }

    # --- 2. 清理 SHA256 ---
    $sha256clean = if ($sha256.Length -ge 64) { $sha256.Substring(0, 64) } else { $sha256 }
    $sha256valid = ($sha256clean.Length -eq 64 -and $sha256clean -ne "N/A" -and $sha256clean -match '^[a-fA-F0-9]{64}$')

    # --- 3. 检查已有文件 ---
    if (Test-Path $fullPath -PathType Leaf) {
        if ($sha256valid) {
            Write-Log "[$INDEX/$TOTAL] 校验已有文件: $filename ..."
            try {
                $actual = (Get-FileHash -Path $fullPath -Algorithm SHA256).Hash.ToLower()
                $expect = $sha256clean.ToLower()
                Write-Log "           期望: $expect"
                Write-Log "           实际: $actual"
                if ($actual -eq $expect) {
                    Write-Log "           [OK] 文件完整，跳过下载 ($size)"
                    $SKIP++
                    continue
                } else {
                    Write-Log "           [WARN] SHA256 不匹配，重新下载..."
                    Remove-Item $fullPath -Force
                }
            } catch {
                Write-Log "           [WARN] 无法计算哈希，重新下载..."
                Remove-Item $fullPath -Force
            }
        } else {
            Write-Log "[$INDEX/$TOTAL] 文件已存在: $filename (无有效校验值) - 跳过"
            $SKIP++
            continue
        }
    }

    # --- 4. URL 处理（HF 镜像） ---
    $downloadUrl = $url
    $headerArgs  = @()
    if ($url -like "*huggingface.co*") {
        if ($HfMirror) {
            $downloadUrl = $url -replace [regex]::Escape("https://huggingface.co"), $HfMirror
        }
        if ($HfToken) {
            $headerArgs = @("--header", "Authorization: Bearer $HfToken")
        }
    }

    Write-Log "---------------------------------------------------"
    Write-Log "[$INDEX/$TOTAL] 下载: $filename"
    Write-Log "           路径: $relPath"
    Write-Log "           大小: $size"
    Write-Log "           来源: $($item.source)"
    if ($HfMirror -and $url -like "*huggingface.co*") {
        Write-Log "           镜像: $HfMirror"
    }

    # --- 5. aria2c 参数 ---
    $checksumArg = @()
    if ($sha256valid) {
        $checksumArg = @("--checksum", "sha-256=$sha256clean")
        Write-Log "           校验: SHA256 (启用)"
    } else {
        Write-Log "           校验: 无"
    }

    $aria2Args = @(
        "--max-connection-per-server=16",
        "--split=16",
        "--min-split-size=2M",
        "--continue=true",
        "--max-tries=5",
        "--retry-wait=10",
        "--timeout=600",
        "--check-certificate=false",
        "--auto-file-renaming=false",
        "--allow-overwrite=true",
        "--check-integrity=true",
        "--file-allocation=prealloc",
        "--summary-interval=30",
        "--console-log-level=warn",
        "--show-console-readout=true",
        "--dir", $saveDir,
        "--out", $fileName
    )

    # --- 6. 执行下载 (HF 链接先 ping 直连，不通则用镜像) ---
    if ($url -like "*huggingface.co*" -and $downloadUrl -ne $url) {
        $useOriginal = $false
        try {
            $ping = Invoke-WebRequest -Uri "https://huggingface.co" -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
            if ($ping.StatusCode -eq 200) { $useOriginal = $true }
        } catch { }
        if ($useOriginal) {
            $urls_to_try = @($url)  # 直连通，只下载不带镜像
            Write-Log "           直连可达，跳过镜像"
        } else {
            $urls_to_try = @($downloadUrl)  # 直连不通，直接用镜像
            Write-Log "           直连不通，使用镜像"
        }
    } else {
        $urls_to_try = @($downloadUrl)
    }

    $exitCode = 1
    $startTime = Get-Date
    foreach ($tryUrl in $urls_to_try) {
        $isMirror = ($tryUrl -ne $url)
        if ($isMirror) {
            Write-Log "           直连失败，尝试镜像..."
        }
        Write-Log "           aria2c 启动中..."
        try {
            $allArgs = $aria2Args + $checksumArg + $headerArgs + @($tryUrl)
            & $ARIA2_EXE $allArgs
            $exitCode = $LASTEXITCODE
        } catch {
            Write-Log "           [ERROR] aria2c 执行异常: $_"
            $exitCode = 1
        }
        if ($exitCode -eq 0) { break }
        # 清理失败的文件，准备重试
        if ($isMirror -eq $false) {
            Remove-Item $fullPath -Force -ErrorAction SilentlyContinue
            Remove-Item "$fullPath.aria2" -Force -ErrorAction SilentlyContinue
        }
    }

    $duration = [math]::Round(((Get-Date) - $startTime).TotalSeconds, 1)

    if ($exitCode -eq 0) {
        $via = if ($downloadUrl -ne $url) { " (镜像)" } else { "" }
        Write-Log "           [OK] 下载完成: $filename (${duration}s)$via"
        $SUCCESS++
    } else {
        Write-Log "           [FAIL] 下载失败: $filename (${duration}s, 退出码: $exitCode)"
        $FAIL++
        Remove-Item $fullPath -Force -ErrorAction SilentlyContinue
        Remove-Item "$fullPath.aria2" -Force -ErrorAction SilentlyContinue
    }
}

# ================= 汇总 =================
Write-Log ""
Write-Log "==================================================="
Write-Log "  下载完成！"
Write-Log "==================================================="
Write-Log "  总数: $TOTAL"
Write-Log "  成功: $SUCCESS"
Write-Log "  跳过: $SKIP"
Write-Log "  失败: $FAIL"
Write-Log "==================================================="

if ($FAIL -gt 0) { exit 1 } else { exit 0 }
