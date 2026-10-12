param(
  [string]$Title = "WinMCP Bench Fixture",
  [string]$Log = "",
  [int]$X = 200,
  [int]$Y = 150,
  [string]$Button1 = "Commit",
  [string]$Button2 = "Alt",
  [string]$InputName = "Bench Input",
  [string]$Button1Name = "Bench Commit",
  [string]$Button2Name = "Bench Alt",
  [string]$StatusName = "Bench Status"
)

# Disposable bench fixture (Phase D). Every observable effect is appended to
# $Log as one JSON object per line by the fixture process itself - that file
# is the external oracle the runner asserts against (never the server reply).

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$script:utf8 = New-Object System.Text.UTF8Encoding($false)

function Write-Event([string]$kind, [string]$value) {
  if ($Log -eq "") { return }
  $payload = @{ event = $kind; t = (Get-Date).ToUniversalTime().ToString("o") }
  if ($value) { $payload.value = $value }
  [IO.File]::AppendAllText($Log, ($payload | ConvertTo-Json -Compress) + "`n", $script:utf8)
}

$script:benchForm = New-Object System.Windows.Forms.Form
$script:benchForm.Text = $Title
$script:benchForm.Name = 'McpBenchForm'
$script:benchForm.Width = 460
$script:benchForm.Height = 260
$script:benchForm.StartPosition = 'Manual'
$script:benchForm.Location = New-Object System.Drawing.Point($X, $Y)
$script:benchForm.TopMost = $true

$script:benchInput = New-Object System.Windows.Forms.TextBox
$script:benchInput.Name = 'BenchTextBox'
$script:benchInput.AccessibleName = $InputName
$script:benchInput.Text = 'initial'
$script:benchInput.Font = New-Object System.Drawing.Font('Segoe UI', 14)
$script:benchInput.SetBounds(20, 20, 400, 34)

$script:benchButton1 = New-Object System.Windows.Forms.Button
$script:benchButton1.Name = 'BenchCommit'
$script:benchButton1.AccessibleName = $Button1Name
$script:benchButton1.Text = $Button1
$script:benchButton1.SetBounds(20, 70, 190, 44)

$script:benchButton2 = New-Object System.Windows.Forms.Button
$script:benchButton2.Name = 'BenchAlt'
$script:benchButton2.AccessibleName = $Button2Name
$script:benchButton2.Text = $Button2
$script:benchButton2.SetBounds(230, 70, 190, 44)

$script:benchStatus = New-Object System.Windows.Forms.Label
$script:benchStatus.Name = 'BenchStatus'
$script:benchStatus.AccessibleName = $StatusName
$script:benchStatus.Text = 'Ready'
$script:benchStatus.Font = New-Object System.Drawing.Font('Segoe UI', 16)
$script:benchStatus.SetBounds(20, 130, 400, 40)

$script:benchInput.Add_TextChanged({ Write-Event 'text' $script:benchInput.Text })
$script:benchButton1.Add_MouseDown({ Write-Event 'b1_down' '' })
$script:benchButton1.Add_MouseUp({ Write-Event 'b1_up' '' })
$script:benchButton2.Add_MouseDown({ Write-Event 'b2_down' '' })
$script:benchButton2.Add_MouseUp({ Write-Event 'b2_up' '' })
$script:benchButton1.Add_Click({
  $script:benchStatus.Text = 'Committed: ' + $script:benchInput.Text
  $script:benchStatus.AccessibleName = $StatusName + ' | committed'
  Write-Event 'button1' ('Committed: ' + $script:benchInput.Text)
})
$script:benchButton2.Add_Click({
  $script:benchStatus.Text = 'Alt clicked'
  $script:benchStatus.AccessibleName = $StatusName + ' | alt'
  Write-Event 'button2' 'Alt clicked'
})
$script:benchForm.Add_MouseDown({ Write-Event 'mouse' '' })

$script:benchForm.Controls.Add($script:benchInput)
$script:benchForm.Controls.Add($script:benchButton1)
$script:benchForm.Controls.Add($script:benchButton2)
$script:benchForm.Controls.Add($script:benchStatus)
$script:benchForm.Add_Shown({ $script:benchForm.Activate() })
[void]$script:benchForm.ShowDialog()
