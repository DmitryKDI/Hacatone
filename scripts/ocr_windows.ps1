param(
    [Parameter(Mandatory = $true)][string[]]$ImagePath,
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [string]$Language = 'ru'
)

# Run with Windows PowerShell 5.1. OCR output is private and is not a visual approval.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.FileAccessMode, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]

$asTask = @([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]
function Await-Operation($Operation, [Type]$ResultType) {
    $task = $asTask.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    $task.GetAwaiter().GetResult()
}

$privateRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../corpus'))
$outputRoot = [IO.Path]::GetFullPath($OutputDirectory)
if (-not $outputRoot.StartsWith($privateRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'OCR output must stay inside the ignored project corpus directory.'
}
[IO.Directory]::CreateDirectory($outputRoot) | Out-Null
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new($Language))
if ($null -eq $engine) { throw "Windows OCR language is unavailable: $Language" }
$utf8 = [Text.UTF8Encoding]::new($false)
foreach ($image in $ImagePath) {
    $resolvedImage = (Resolve-Path -LiteralPath $image).Path
    $file = Await-Operation ([Windows.Storage.StorageFile]::GetFileFromPathAsync($resolvedImage)) ([Windows.Storage.StorageFile])
    $stream = Await-Operation ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    try {
        $decoder = Await-Operation ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await-Operation ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        try {
            $result = Await-Operation ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
            $lines = @($result.Lines | ForEach-Object {
                @{text = $_.Text; words = @($_.Words | ForEach-Object {
                    @{text = $_.Text; x = $_.BoundingRect.X; y = $_.BoundingRect.Y;
                      width = $_.BoundingRect.Width; height = $_.BoundingRect.Height}
                })}
            })
            $base = [IO.Path]::GetFileNameWithoutExtension($resolvedImage)
            [IO.File]::WriteAllText((Join-Path $outputRoot "$base.txt"), (($result.Lines | ForEach-Object Text) -join "`n"), $utf8)
            [IO.File]::WriteAllText((Join-Path $outputRoot "$base.json"), (@{engine = 'Windows.Media.Ocr'; language = $Language; lines = $lines} | ConvertTo-Json -Depth 7), $utf8)
            Write-Output "$base : $($lines.Count) lines, not yet visually verified"
        } finally { $bitmap.Dispose() }
    } finally { $stream.Dispose() }
}
