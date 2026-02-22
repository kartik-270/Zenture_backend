import re
from collections import defaultdict
with open('routes.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()
routes = defaultdict(list)
for i, line in enumerate(lines):
    if line.strip().startswith('#'): continue
    match = re.search(r'@\w+_bp\.route\([\'\"]([^\'\"]+)[\'\"](?:,\s*methods=\[([^\]]+)\])?\)', line)
    if match:
        path = match.group(1)
        methods = match.group(2) if match.group(2) else 'GET'
        methods = [m.strip(' \'\"').upper() for m in methods.split(',')] if methods != 'GET' else ['GET']
        for m in methods:
            routes[f'{m} {path}'].append(i + 1)
duplicates = {k: v for k, v in routes.items() if len(v) > 1}
if duplicates:
    print('Duplicates found:')
    for k, v in duplicates.items():
        print(f'{k} at lines {v}')
else:
    print('No duplicates found.')

