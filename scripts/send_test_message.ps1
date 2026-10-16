# Sends one test WhatsApp text from the Meta test number to your phone.
# Run:  powershell -ExecutionPolicy Bypass -File scripts\send_test_message.ps1
# Reply "hi" to the test number from your phone first (WhatsApp's 24-hour rule).

$phoneId = Read-Host "Phone number ID (press Enter for 1414805245056988)"
if (-not $phoneId) { $phoneId = "1414805245056988" }
$token = Read-Host "Paste the access token from Meta"
$to = Read-Host "Your WhatsApp number (example 919847012345, no + or spaces)"

$body = @{
    messaging_product = "whatsapp"
    to                = $to.Trim().TrimStart("+").Replace(" ", "")
    type              = "text"
    text              = @{ body = "Hello from the True Choice bot test" }
} | ConvertTo-Json -Depth 5

try {
    $result = Invoke-RestMethod -Method Post `
        -Uri "https://graph.facebook.com/v25.0/$($phoneId.Trim())/messages" `
        -Headers @{ Authorization = "Bearer $($token.Trim())" } `
        -ContentType "application/json" -Body $body
    Write-Host "SUCCESS - check your WhatsApp. Message id: $($result.messages[0].id)" -ForegroundColor Green
}
catch {
    Write-Host "FAILED:" -ForegroundColor Red
    if ($_.ErrorDetails.Message) { Write-Host $_.ErrorDetails.Message } else { Write-Host $_ }
}
