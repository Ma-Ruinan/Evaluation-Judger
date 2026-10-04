param([Parameter(Mandatory=$true)][string]$InputPath,
      [Parameter(Mandatory=$true)][string]$OutputFolder)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$alreadyRunning = @(Get-Process POWERPNT -ErrorAction SilentlyContinue).Count -gt 0
$application = New-Object -ComObject PowerPoint.Application
$presentation = $null
$oldSecurity = $application.AutomationSecurity
$oldAlerts = $application.DisplayAlerts
try {
    $application.AutomationSecurity = 3
    $application.DisplayAlerts = 1
    $presentation = $application.Presentations.Open($InputPath, -1, 0, 0)
    $pdf = Join-Path $OutputFolder 'rendered.pdf'
    $presentation.SaveAs($pdf, 32)
    $pages = @()
    foreach ($slide in $presentation.Slides) {
        $texts = @()
        foreach ($shape in $slide.Shapes) {
            if ($shape.HasTextFrame -eq 0 -or $shape.TextFrame.HasText -eq 0) { continue }
            $range = $shape.TextFrame.TextRange
            $left = [double]$range.BoundLeft
            $top = [double]$range.BoundTop
            $width = [double]$range.BoundWidth
            $height = [double]$range.BoundHeight
            $outsideSlide = ($left -lt -2 -or $top -lt -2 -or ($left+$width) -gt ($presentation.PageSetup.SlideWidth+2) -or ($top+$height) -gt ($presentation.PageSetup.SlideHeight+2))
            $outsideBox = ($left -lt ($shape.Left-3) -or $top -lt ($shape.Top-3) -or ($left+$width) -gt ($shape.Left+$shape.Width+3) -or ($top+$height) -gt ($shape.Top+$shape.Height+3))
            $texts += [ordered]@{shape_id=$shape.Id;name=$shape.Name;text=$range.Text;
                bounds=@{left=$left;top=$top;width=$width;height=$height};
                shape_bounds=@{left=[double]$shape.Left;top=[double]$shape.Top;width=[double]$shape.Width;height=[double]$shape.Height};
                rotation=[double]$shape.Rotation;outside_slide=$outsideSlide;outside_shape_candidate=$outsideBox}
        }
        $pages += [ordered]@{slide=$slide.SlideIndex;name=$slide.Name;text_shapes=$texts}
    }
    [ordered]@{renderer='Microsoft PowerPoint';office_version=$application.Version;
        slide_count=$presentation.Slides.Count;slide_width=[double]$presentation.PageSetup.SlideWidth;
        slide_height=[double]$presentation.PageSetup.SlideHeight;units='points';pdf=$pdf;slides=$pages;
        limitation='Text bounds are actual PowerPoint layout measurements after PDF export; potential box overflow requires interpretation. Graphic occlusion and image content require visual review.'} | ConvertTo-Json -Depth 12 -Compress
} finally {
    if ($null -ne $presentation) { $presentation.Close() }
    $application.AutomationSecurity = $oldSecurity
    $application.DisplayAlerts = $oldAlerts
    if (-not $alreadyRunning -and $application.Presentations.Count -eq 0) { $application.Quit() }
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($application) | Out-Null
}
