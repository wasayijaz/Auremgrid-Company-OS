from pathlib import Path
path = Path(r'D:\\Auremgrid\\Auremgrid Company OS\\README.md')
path.write_text('''# Auremgrid Company OS\n\nPLACEHOLDER\n''', encoding='utf-8')
print(path.stat().st_size)
