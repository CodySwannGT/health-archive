"""Validate explicit patient corrections without changing earlier source records."""
from datetime import datetime


def measurement_supersessions(records):
    indexed = {r['record_id']: r for r in records}
    superseded = {}
    for row in records:
        review = row.get('details', {}).get('supersedes_measurement')
        if not review:
            continue
        prior = indexed.get(review['record_id'])
        if (not prior or prior['record_type'] != 'anthropometric_measurement'
                or row['record_type'] != 'anthropometric_measurement'
                or prior['patient_scope'] != row['patient_scope']
                or prior['content_sha256'] != review['content_sha256']):
            raise ValueError('Measurement correction must bind to an exact prior patient measurement')
        old, new = prior['details'], row['details']
        if any(old.get(k) != new.get(k) for k in ('metric', 'unit')) or old.get('occurrence', {}).get('date') != new.get('occurrence', {}).get('date'):
            raise ValueError('Measurement correction must describe the same metric, unit and occurrence date')
        if datetime.fromisoformat(row['reported_at'].replace('Z', '+00:00')) <= datetime.fromisoformat(prior['reported_at'].replace('Z', '+00:00')):
            raise ValueError('Measurement correction must be reported after the original')
        if prior['record_id'] in superseded:
            raise ValueError('Conflicting measurement corrections require review, or an explicit correction chain')
        superseded[prior['record_id']] = row['record_id']
    return superseded
