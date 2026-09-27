param(
    [string]$Draft = "D:\FINAL\final-hack\.local\template-work\EFQA-template-draft.pptx",
    [string]$Output = "D:\FINAL\final-hack\docs\defense",
    [string]$Review = "D:\FINAL\final-hack\.local\template-final-review"
)
$ErrorActionPreference = "Stop"
$font = "D:\FINAL\final-hack\.local\template-work\EFQA-Oswald.ttf"
if (-not (Test-Path -LiteralPath $Draft)) { throw "Build the draft first." }
if (-not (Test-Path -LiteralPath $Output)) { throw "Output parent is missing." }
if (-not (Test-Path -LiteralPath (Split-Path -Parent $Review))) { throw "Review parent is missing." }
if (-not (Test-Path -LiteralPath $Review)) { New-Item -ItemType Directory -Path $Review | Out-Null }
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class EFQATemplateFonts {
    [DllImport("gdi32.dll", CharSet=CharSet.Unicode)]
    public static extern int AddFontResourceEx(string filename, uint flags, IntPtr reserved);
    [DllImport("gdi32.dll", CharSet=CharSet.Unicode)]
    public static extern bool RemoveFontResourceEx(string filename, uint flags, IntPtr reserved);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    public static extern IntPtr SendMessage(IntPtr hwnd, uint msg, IntPtr wparam, IntPtr lparam);
}
"@
$added = [EFQATemplateFonts]::AddFontResourceEx($font, 0, [IntPtr]::Zero)
if ($added -eq 0) { throw "Could not load the Cyrillic headline font." }
[void][EFQATemplateFonts]::SendMessage([IntPtr]0xffff, 0x001D, [IntPtr]::Zero, [IntPtr]::Zero)
$app = $null
$deck = $null
try {
    $app = New-Object -ComObject PowerPoint.Application
    $deck = $app.Presentations.Open($Draft, -1, 0, 0)
    # Embed the OFL headline font; fonts that prohibit embedding remain installed-font references.
    $deck.SaveAs((Join-Path $Output "presentation.pptx"), 24, -1)
    $deck.SaveAs((Join-Path $Output "presentation.pdf"), 32)
    for ($n = 1; $n -le $deck.Slides.Count; $n++) {
        $deck.Slides.Item($n).Export((Join-Path $Review ("slide-{0:D2}.png" -f $n)), "PNG", 1440, 810)
    }
    "PowerPoint exported $($deck.Slides.Count) slides, PDF, PPTX and individual review images."
} finally {
    if ($null -ne $deck) { $deck.Close() }
    if ($null -ne $app) { $app.Quit() }
    [void][EFQATemplateFonts]::RemoveFontResourceEx($font, 0, [IntPtr]::Zero)
}
