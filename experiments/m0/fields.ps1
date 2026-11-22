# Query only handles created by this probe. Both test windows stay hidden.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms,UIAutomationClient,UIAutomationTypes
$firstWindow = [Windows.Forms.Form]::new()
$secondWindow = [Windows.Forms.Form]::new()
$first = [Windows.Forms.TextBox]::new()
$second = [Windows.Forms.TextBox]::new()
$password = [Windows.Forms.TextBox]::new()
$password.UseSystemPasswordChar = $true
$firstWindow.Controls.AddRange(@($first, $second, $password))
$first.Text = 'alpha alpha'
try {
    $ids = foreach ($control in @($first, $second, $password)) {
        $element = [Windows.Automation.AutomationElement]::FromHandle($control.Handle)
        $value = $null
        $text = $null
        $hasValue = $element.TryGetCurrentPattern([Windows.Automation.ValuePattern]::Pattern, [ref]$value)
        $hasText = $element.TryGetCurrentPattern([Windows.Automation.TextPattern]::Pattern, [ref]$text)
        Write-Host ("own={0} password={1} value={2} text={3}" -f ($element.Current.ProcessId -eq $PID),
            $element.Current.IsPassword, $hasValue, $hasText)
        $element.GetRuntimeId() -join ','
    }
    Write-Host ("distinct fields={0}, distinct windows={1}" -f (@($ids | Select-Object -Unique).Count),
        ($firstWindow.Handle -ne $secondWindow.Handle))
    $element = [Windows.Automation.AutomationElement]::FromHandle($first.Handle)
    $pattern = $element.GetCurrentPattern([Windows.Automation.TextPattern]::Pattern)
    foreach ($start in @(0, 6)) {
        $first.Select($start, 5)
        $range = $pattern.GetSelection()[0]
        $prefix = $pattern.DocumentRange.Clone()
        $prefix.MoveEndpointByRange([Windows.Automation.Text.TextPatternRangeEndpoint]::End,
            $range, [Windows.Automation.Text.TextPatternRangeEndpoint]::Start)
        Write-Host ("offset={0} length={1} matches={2}" -f $prefix.GetText(-1).Length,
            $range.GetText(-1).Length, ($range.GetText(-1) -eq 'alpha'))
    }
    $first.Dispose()
    try { $null = $element.Current.ProcessId; Write-Host 'Stale element still reports a process ID' }
    catch { Write-Host 'Stale element rejected' }
} finally {
    $firstWindow.Dispose()
    $secondWindow.Dispose()
}
