$ProjectDir = $PSScriptRoot
Set-Location $ProjectDir

Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$ProjectDir'; python -m uvicorn api:app --port 8000 --reload"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$ProjectDir'; python -m uvicorn ui:app --port 8501 --reload"
