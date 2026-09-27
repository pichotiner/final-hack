$ErrorActionPreference = "Stop"
$out = $PSScriptRoot
$root = Split-Path -Parent (Split-Path -Parent $out)
$review = Join-Path $root ".local\minimal-gradient-reviewed"
if (-not (Test-Path -LiteralPath (Join-Path $root ".local"))) { throw "Missing local workspace" }
if (-not (Test-Path -LiteralPath $review)) { New-Item -ItemType Directory -Path $review | Out-Null }
$app = $null
$deck = $null
try {
    $app = New-Object -ComObject PowerPoint.Application
    $deck = $app.Presentations.Open((Join-Path $out "presentation-minimal-gradient.pptx"), -1, 0, 0)
    $deck.SaveAs((Join-Path $out "presentation-minimal-gradient.pdf"), 32)
    for ($n = 1; $n -le $deck.Slides.Count; $n++) {
        $deck.Slides.Item($n).Export((Join-Path $review ("slide-{0:D2}.png" -f $n)), "PNG", 1440, 810)
    }
    "Exported $($deck.Slides.Count) slides from Microsoft PowerPoint."
} finally {
    if ($null -ne $deck) { $deck.Close() }
    if ($null -ne $app) { $app.Quit() }
}
