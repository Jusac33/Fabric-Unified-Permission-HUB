param(
  [string]$BindHost = "127.0.0.1",
  [int]$Port = 8000
)
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned -Force | Out-Null
& "$PSScriptRoot\.venv\Scripts\Activate.ps1"
python -m pip install -r "$PSScriptRoot\requirements.txt" | Out-Null
uvicorn app.main:app --host $BindHost --port $Port --reload
