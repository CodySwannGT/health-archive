"""Repair AGE graph catalog OIDs after restoring this archive into a fresh database.

Workaround documented by Apache AGE issue 2503. This changes catalog identities,
not graph entities or clinical records. Always run within one transaction.
"""
from psycopg import sql
from common import connect, json_out


def repair_restored_age(conn):
    mismatches=conn.execute('SELECT name,graphid,namespace::oid AS restored_oid FROM ag_catalog.ag_graph WHERE graphid<>namespace::oid').fetchall()
    if not mismatches:return {'repaired_graphs':0}
    if len(mismatches)!=1 or mismatches[0]['name']!='health_graph':
        raise RuntimeError('Unexpected graph catalog. Review before applying this single-graph restore repair.')
    fk=conn.execute("SELECT conname,pg_get_constraintdef(oid) AS definition FROM pg_constraint WHERE conrelid='ag_catalog.ag_label'::regclass AND confrelid='ag_catalog.ag_graph'::regclass AND contype='f'").fetchall()
    if len(fk)!=1:raise RuntimeError('Unexpected AGE graph foreign-key definition')
    constraint=fk[0]
    conn.execute(sql.SQL('ALTER TABLE ag_catalog.ag_label DROP CONSTRAINT {}').format(sql.Identifier(constraint['conname'])))
    graph=mismatches[0]
    conn.execute('UPDATE ag_catalog.ag_label SET graph=%s WHERE graph=%s',(graph['restored_oid'],graph['graphid']))
    conn.execute('UPDATE ag_catalog.ag_graph SET graphid=%s WHERE name=%s',(graph['restored_oid'],graph['name']))
    conn.execute(sql.SQL('ALTER TABLE ag_catalog.ag_label ADD CONSTRAINT {} {}').format(sql.Identifier(constraint['conname']),sql.SQL(constraint['definition'])))
    return {'repaired_graphs':1,'clinical_and_graph_rows_changed':0}


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',required=True)
    args=parser.parse_args()
    with connect(admin=True,database=args.database) as conn:
        json_out(repair_restored_age(conn))
