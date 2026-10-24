"""Create a complete PostgreSQL backup, including source bytes and graph/vector data."""
from datetime import datetime,timezone
import hashlib
import os
from common import DB
from pg_tools import run_pg_tool


def backup():
    folder=DB/'backups';folder.mkdir(exist_ok=True,mode=0o700)
    os.chmod(folder,0o700)
    path=folder/('health_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.dump')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'wb') as stream:
            run_pg_tool('pg_dump', ['-Fc'], stdout=stream)
    except BaseException:
        path.unlink(missing_ok=True);raise
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    sidecar = path.with_name(path.name + '.sha256')
    fd = os.open(sidecar, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(checksum + '  ' + path.name + '\n')
    return {'path':str(path),'bytes':path.stat().st_size,'sha256':checksum}
