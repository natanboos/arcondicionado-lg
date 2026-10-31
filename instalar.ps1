# Instala as dependências e cria o atalho "Ar-condicionado LG" na Área de Trabalho.
# Uso (na pasta do projeto):  powershell -ExecutionPolicy Bypass -File .\instalar.ps1

$ErrorActionPreference = "Stop"
$dir = $PSScriptRoot

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host "Python não encontrado. Instale o Python 3.10 ou mais novo em https://www.python.org/downloads/" -ForegroundColor Red
    Write-Host "(marque 'Add python.exe to PATH' na instalação) e rode este script de novo."
    exit 1
}

Write-Host "Instalando dependências..." -ForegroundColor Cyan
& $python.Source -m pip install --upgrade -r (Join-Path $dir "requirements.txt")

$pythonw = Join-Path (Split-Path $python.Source) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = (Get-Command pythonw).Source }

# o ícone é gerado pelo app na primeira execução; gera agora para o atalho já ter ícone
$icon = Join-Path $dir "ui\icon.ico"
if (-not (Test-Path $icon)) {
    & $python.Source -c "import sys; sys.path.insert(0, r'$dir'); from app import make_icon_image, ICON_FILE; make_icon_image(256).save(ICON_FILE, sizes=[(16,16),(32,32),(48,48),(256,256)])"
}

$desktop = [Environment]::GetFolderPath("Desktop")
$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut("$desktop\Ar-condicionado LG.lnk")
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = "`"$dir\app.py`""
$shortcut.WorkingDirectory = $dir
$shortcut.IconLocation = $icon
$shortcut.Description = "Controle do ar-condicionado LG ThinQ"
$shortcut.Save()

Write-Host "Pronto! Abra o atalho 'Ar-condicionado LG' na Área de Trabalho." -ForegroundColor Green
