"""Stage only container code for upload, excluding credentials and health sources."""
import argparse
import json
from pathlib import Path
import shutil

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('destination', type=Path)
parser.add_argument('--backup', action='store_true', help='Stage the daily cron worker configuration')
args = parser.parse_args()
source = Path(__file__).resolve().parents[1]
dest = args.destination.resolve()
dest.mkdir(parents=True, exist_ok=True, mode=0o700)
for relative in ('Dockerfile', '.dockerignore', 'railway.json', 'railway/entrypoint.sh',
                 'railway/backup.sh', 'railway/pg_hba.conf'):
    target = dest / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source / relative, target)
if args.backup:
    config = json.loads((dest / 'railway.json').read_text())
    config['deploy'] = {'cronSchedule': '0 7 * * *', 'restartPolicyType': 'NEVER'}
    (dest / 'railway.json').write_text(json.dumps(config, indent=2) + '\n')
print(f'Staged six container files in {dest}. No archive or credential files copied.')
