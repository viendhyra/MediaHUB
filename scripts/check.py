"""Проверки пакета: Python, встроенный JavaScript и целостность приложения."""
import ast,hashlib,json,subprocess,tempfile
from pathlib import Path
root=Path(__file__).resolve().parents[1]
for name in ('app.py','cache_refresh.py','system_setup.py','download_organizer.py'):
    ast.parse((root/name).read_text(encoding='utf-8'),filename=name)
html=(root/'templates/index.html').read_text(encoding='utf-8')
script=html.split('<script>',1)[1].split('</script>',1)[0]
with tempfile.TemporaryDirectory() as folder:
    path=Path(folder)/'portal.js';path.write_text(script,encoding='utf-8')
    subprocess.run(['node','--check',str(path)],check=True)
manifest=json.loads((root/'static/android.json').read_text(encoding='utf-8'))
apk=root/'static'/manifest['filename']
assert hashlib.sha256(apk.read_bytes()).hexdigest()==manifest['sha256'],'APK SHA256 mismatch'
assert apk.stat().st_size==manifest['size'],'APK size mismatch'
assert (root/'VERSION.txt').read_text(encoding='utf-8').strip() in (root/'CHANGELOG.md').read_text(encoding='utf-8'),'Missing changelog version'
print('Python, JavaScript, APK и версия: OK')
