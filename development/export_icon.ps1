# Export the approved transparent artwork without changing the source design.
[CmdletBinding()]
param([string]$SourceArtwork = 'assets\free-ai-openmark.png')

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
Add-Type -ReferencedAssemblies System.Drawing -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Drawing;

public static class FreeAIIconResource {
    [DllImport("user32.dll", SetLastError=true)]
    private static extern IntPtr CreateIconFromResourceEx(
        byte[] resource, uint size, bool icon, uint version, int width, int height, uint flags);

    [DllImport("user32.dll")]
    private static extern bool DestroyIcon(IntPtr icon);

    public static bool Check(byte[] frame, int size) {
        IntPtr handle = CreateIconFromResourceEx(frame, (uint)frame.Length, true, 0x00030000, size, size, 0);
        if (handle == IntPtr.Zero) return false;
        try {
            using (Icon icon = Icon.FromHandle(handle))
                return icon.Width == size && icon.Height == size;
        } finally {
            DestroyIcon(handle);
        }
    }
}
"@

$iconRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))

function Assert-IconPath {
    param([string]$Path)
    $fullPath = [IO.Path]::GetFullPath($Path)
    if (-not $fullPath.StartsWith($iconRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Icon path is outside the FreeAI directory: $fullPath"
    }
    $itemPath = $fullPath
    while ($itemPath) {
        if ((Test-Path -LiteralPath $itemPath) -and
            ((Get-Item -Force -LiteralPath $itemPath).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Icon path contains a reparse point: $itemPath"
        }
        $itemPath = [IO.Path]::GetDirectoryName($itemPath)
    }
}

function Convert-IconFrame {
    param([System.Drawing.Image]$Source, [int]$Size)
    $bitmap = [System.Drawing.Bitmap]::new($Size, $Size, [Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $attributes = [System.Drawing.Imaging.ImageAttributes]::new()
    $stream = [IO.MemoryStream]::new()
    try {
        $graphics.Clear([System.Drawing.Color]::Transparent)
        $graphics.CompositingMode = [Drawing.Drawing2D.CompositingMode]::SourceCopy
        $graphics.InterpolationMode = [Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $graphics.PixelOffsetMode = [Drawing.Drawing2D.PixelOffsetMode]::HighQuality
        $attributes.SetWrapMode([Drawing.Drawing2D.WrapMode]::TileFlipXY)
        $rectangle = [System.Drawing.Rectangle]::new(0, 0, $Size, $Size)
        $graphics.DrawImage($Source, $rectangle, 0, 0, $Source.Width, $Source.Height,
                            [System.Drawing.GraphicsUnit]::Pixel, $attributes)
        if ($bitmap.GetPixel(0, 0).A -eq 255) {
            throw "Expected a transparent corner in the ${Size}px icon."
        }
        $bitmap.Save($stream, [Drawing.Imaging.ImageFormat]::Png)
        return ,$stream.ToArray()
    }
    finally {
        $stream.Dispose()
        $attributes.Dispose()
        $graphics.Dispose()
        $bitmap.Dispose()
    }
}

function New-IconBundle {
    param([int[]]$Sizes, [hashtable]$Frames)
    $stream = [IO.MemoryStream]::new()
    $writer = [IO.BinaryWriter]::new($stream)
    try {
        $writer.Write([uint16]0)
        $writer.Write([uint16]1)
        $writer.Write([uint16]$Sizes.Count)
        $offset = 6 + 16 * $Sizes.Count
        foreach ($size in $Sizes) {
            $dimension = [byte]($size % 256)
            $writer.Write($dimension)
            $writer.Write($dimension)
            $writer.Write([uint16]0)
            $writer.Write([uint16]1)
            $writer.Write([uint16]32)
            $writer.Write([uint32]$Frames[$size].Length)
            $writer.Write([uint32]$offset)
            $offset += $Frames[$size].Length
        }
        foreach ($size in $Sizes) {
            $writer.Write([byte[]]$Frames[$size])
        }
        $writer.Flush()
        return ,$stream.ToArray()
    }
    finally {
        $writer.Dispose()
        $stream.Dispose()
    }
}

function Test-IconBundle {
    param([byte[]]$Bytes, [int[]]$Sizes)
    $stream = [IO.MemoryStream]::new($Bytes)
    try {
        $icon = [System.Drawing.Icon]::new($stream, 32, 32)
        try {
            if ($icon.Width -ne 32 -or $icon.Height -ne 32) {
                throw 'System.Drawing could not open the ICO bundle.'
            }
        }
        finally {
            $icon.Dispose()
        }
        for ($index = 0; $index -lt $Sizes.Count; $index++) {
            $size = $Sizes[$index]
            $entry = 6 + 16 * $index
            $length = [BitConverter]::ToUInt32($Bytes, $entry + 8)
            $offset = [BitConverter]::ToUInt32($Bytes, $entry + 12)
            $frame = [byte[]]::new($length)
            [Array]::Copy($Bytes, $offset, $frame, 0, $length)
            $frameStream = [IO.MemoryStream]::new($frame)
            $image = [System.Drawing.Image]::FromStream($frameStream)
            try {
                if ($image.Width -ne $size -or $image.Height -ne $size -or
                    -not [System.Drawing.Image]::IsAlphaPixelFormat($image.PixelFormat) -or
                    -not [FreeAIIconResource]::Check($frame, $size)) {
                    throw "Windows did not read the requested ${size}px transparent icon frame."
                }
            }
            finally {
                $image.Dispose()
                $frameStream.Dispose()
            }
        }
    }
    finally {
        $stream.Dispose()
    }
}

function Write-IconFile {
    param([string]$Path, [byte[]]$Bytes)
    Assert-IconPath $Path
    $staged = Join-Path ([IO.Path]::GetDirectoryName($Path)) ('.export-icon-' + [Guid]::NewGuid().ToString('N'))
    Assert-IconPath $staged
    try {
        [IO.File]::WriteAllBytes($staged, $Bytes)
        if ([IO.File]::Exists($Path)) {
            [IO.File]::Replace($staged, $Path, [System.Management.Automation.Language.NullString]::Value)
        }
        else {
            [IO.File]::Move($staged, $Path)
        }
    }
    finally {
        if ([IO.File]::Exists($staged)) {
            [IO.File]::Delete($staged)
        }
    }
}

$sourcePath = if ([IO.Path]::IsPathRooted($SourceArtwork)) {
    [IO.Path]::GetFullPath($SourceArtwork)
} else {
    [IO.Path]::GetFullPath((Join-Path $iconRoot $SourceArtwork))
}
$iconPath = Join-Path $iconRoot 'assets\free-ai.ico'
$pngPath = Join-Path $iconRoot 'assets\free-ai-ui.png'
$backupDirectory = Join-Path $iconRoot 'development\backups\before-blue-brand'
$outputPaths = @($iconPath, $pngPath)
foreach ($path in @($sourcePath, $iconPath, $pngPath, $backupDirectory)) {
    Assert-IconPath $path
}
if ($sourcePath -in $outputPaths) {
    throw 'The source must be separate from the exported icon and UI PNG.'
}

$sizes = @(16, 24, 32, 48, 64, 128, 256)
$frames = @{}
$source = [System.Drawing.Image]::FromFile($sourcePath)
try {
    if ($source.Width -ne $source.Height -or $source.Width -lt 256 -or
        -not [System.Drawing.Image]::IsAlphaPixelFormat($source.PixelFormat)) {
        throw 'Expected square transparent source artwork at least 256px wide.'
    }
    foreach ($size in $sizes) {
        $frames[$size] = Convert-IconFrame -Source $source -Size $size
    }
}
finally {
    $source.Dispose()
}

$iconBytes = New-IconBundle -Sizes $sizes -Frames $frames
Test-IconBundle -Bytes $iconBytes -Sizes $sizes

foreach ($path in $outputPaths) {
    $backupPath = Join-Path $backupDirectory ([IO.Path]::GetFileName($path))
    Assert-IconPath $backupPath
    if ([IO.File]::Exists($path) -and -not [IO.File]::Exists($backupPath)) {
        $null = [IO.Directory]::CreateDirectory($backupDirectory)
        [IO.File]::Copy($path, $backupPath)
    }
}
Write-IconFile -Path $iconPath -Bytes $iconBytes
Write-IconFile -Path $pngPath -Bytes $frames[64]
Test-IconBundle -Bytes ([IO.File]::ReadAllBytes($iconPath)) -Sizes $sizes

[pscustomobject]@{ Path = $iconPath; Sizes = $sizes -join ', '; Bytes = $iconBytes.Length }
[pscustomobject]@{ Path = $pngPath; Sizes = '64'; Bytes = $frames[64].Length }
