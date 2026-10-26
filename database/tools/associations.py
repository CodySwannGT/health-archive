"""The connections engine: evidence-graded associations in one patient's dated records.

Pure functions over facts that `load_facts` reads from the database, so
projection, verification, read tools and tests apply the same rules. An association is a computed pattern with a
stated method, strength, supporting and contrary records, competing
explanations and limitations. It is never a cause, a diagnosis or advice.

Methods:
- exposure_episode_contrast: how often a symptom occurs on days with and
  without an exposure (same day or the day after), from explicit present and
  absent reports. Days with no report are unknown, never absent.
- constant_background: an exposure present on every observed day, symptom-free
  days included, or stopped before every observed day, cannot explain why a
  symptom comes and goes. It may still affect susceptibility.
- exposure_series_change: how a measurement series moves while a medication is
  on versus off, across its starts, stops and restarts.
"""
from datetime import date, timedelta
import hashlib
import json
import math
import os
import re

STRENGTHS = ('insufficient', 'weak', 'moderate', 'strong')
STATEMENTS = {
    'insufficient': 'There is not enough data yet to tell whether {e} relates to {o}.',
    'weak': '{e} may be associated with {o}; so far this is a timing pattern only.',
    'moderate': '{e} may be contributing to {o}.',
    'strong': '{e} is consistently associated with {o} in these records.',
}
CAUTION = 'A computed association in self-reported records, not proof of cause.'
ACTIVE = {'active'}
INACTIVE = {'stopped', 'on-hold', 'not-taken', 'not_listed', 'planned_discontinuation_completed'}
CHANGE_STATUS = {'started': 'active', 'restarted': 'active', 'regimen_changed': 'active', 'stopped': 'stopped', 'paused': 'on-hold'}
# Medications whose state changed this long before an outcome's first observed
# day are compared against it; older stops are not plausible background.
RECENT_STOP_DAYS = 30
SERIES_LOOKBACK_DAYS = 60
# A before/after comparison needs a baseline this close before the change, and
# at least a week of measurements after it to be more than insufficient.
BASELINE_DAYS = 14
FOLLOW_UP_DAYS = 7
# A pooled pattern needs at least this many distinct opted-in patients. The
# database enforces the same floor; configuration can only raise it.
MIN_CONTRIBUTORS = max(5, int(os.environ.get('HEALTH_POOL_MIN_CONTRIBUTORS', '5')))
CONSENT_VERSION = '2026-10-26'
CONSENT_TEXT = (
    'Opting in adds the associations computed from this chart to pooled patterns that help the assistant recognize '
    'patterns for every user. Only each association\'s method, the shared names of its exposure and outcome, its '
    'direction, strength and effect size are pooled; records, dates, notes, quotes and identifiers never leave the chart. '
    'A pattern is stored only when at least five people share it. Pooled patterns are not published or sold, and are used '
    'only to give the assistant context. Opting out removes this chart\'s contribution at once.')


def association_id(method, exposure, outcome, extra=''):
    return 'assoc_' + hashlib.sha256(json.dumps([method, exposure, outcome, extra]).encode()).hexdigest()[:24]


def pool_key(concept_id, basis, family, label):
    """A concept identity shared across patients, or None when the concept is only meaningful to one archive.

    Curated, coded and structured-key concepts are shared vocabularies. Medications
    pool by name, the label without brand, form or dose, because curated medication
    concepts belong to one archive and a code-only identity would miss self-reports."""
    if family != 'medication' and basis in ('curated_registry', 'source_code', 'source_key'):
        return concept_id
    if family == 'medication' and label:
        name = label.casefold().split('(')[0]
        name = re.split(r'\b\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml|iu|units?|%)\b', name)[0]
        name = ' '.join(re.sub(r'[^a-z0-9 -]', ' ', name).split()).strip(' -')
        return f'medication:{name}' if name else None
    return None


def fisher_greater(a, b, c, d):
    """One-sided Fisher exact p-value that exposure raises the outcome's rate."""
    n, row, col = a + b + c + d, a + b, a + c
    total = math.comb(n, col)
    return sum(math.comb(row, k) * math.comb(n - row, col - k) for k in range(a, min(row, col) + 1)) / total if total else 1.0


def risk_ratio(a, b, c, d):
    """Risk ratio with a 0.5 continuity correction, so empty cells stay finite."""
    return ((a + 0.5) / (a + b + 1)) / ((c + 0.5) / (c + d + 1))


def slope_per_week(points, min_points=3, min_span_days=7):
    """Least-squares slope of (date, value) points, per 7 days.

    None for fewer than three points or a span under a week: day-to-day
    fluctuation, such as water weight, would otherwise read as a trend."""
    if len(points) < min_points or (points[-1][0] - points[0][0]).days < min_span_days:
        return None
    xs = [(d - points[0][0]).days for d, _ in points]
    ys = [v for _, v in points]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    return round(7 * sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx, 3)


class Timeline:
    """A medication's known state by day, from its dated changes and regimen."""

    def __init__(self, med):
        self.med = med
        self.events = sorted(med['events'], key=lambda e: e['date'])
        self.since = med.get('known_since')

    def state(self, day):
        prior = [e for e in self.events if e['date'] <= day]
        if prior:
            return 'on' if prior[-1]['status'] in ACTIVE else 'off'
        if self.events and self.events[0].get('previous'):
            return 'on' if self.events[0]['previous'] in ACTIVE else 'off'
        if self.since and day >= self.since:
            return 'on' if self.med['status'] in ACTIVE else 'off' if self.med['status'] in INACTIVE else None
        return None

    def last_change_before(self, day):
        prior = [e for e in self.events if e['date'] <= day]
        return prior[-1] if prior else None


def daily(observations):
    """Per concept and day: present if any record that day says present, else absent; the records behind each."""
    days = {}
    for o in observations:
        key = (o['concept'], o['date'])
        entry = days.setdefault(key, {'present': False, 'absent': False, 'records': [], 'amount': None})
        entry[o['assertion']] = True
        entry['records'].append(o['rid'])
        if o.get('amount') is not None:
            entry['amount'] = max(entry['amount'] or 0, o['amount'])
    result = {}
    for (concept, day), e in days.items():
        state = 'present' if e['present'] else 'absent'
        amount = e['amount'] if e['amount'] is not None else (0 if state == 'absent' else None)
        result.setdefault(concept, {})[day] = {'state': state, 'records': sorted(e['records']), 'amount': amount}
    return result


def grade_contrast(a, b, c, d, rr, p):
    n = a + b + c + d
    if n < 4 or a == 0:
        return 'insufficient'
    if rr >= 2 and p < 0.05 and n >= 8:
        return 'strong'
    if rr >= 2 and a >= 3 and n >= 5:
        return 'moderate'
    return 'weak'


def find_associations(facts):
    """Associations in one patient's facts.

    `facts` holds `observations` (symptom and exposure reports: rid, concept, label,
    family, kind symptom|exposure, date, assertion present|absent, amount),
    `medications` (concept, label, status, known_since, events of date, status,
    previous, change_type, rid), `series` ({concept: {label, unit, points: [(date,
    value, rid)]}}) and `concepts` ({concept_id: (basis, family, label)}).
    """
    concepts = facts.get('concepts', {})
    labels = {}
    for o in facts['observations']:
        labels.setdefault(o['concept'], o['label'])
    for m in facts['medications']:
        labels[m['concept']] = m['label']
    labels.update({c: v[2] for c, v in concepts.items() if v[2]})
    by_day = daily(facts['observations'])
    kinds = {o['concept']: o['kind'] for o in facts['observations']}
    outcomes = {c: days for c, days in by_day.items() if kinds[c] == 'symptom'}
    exposures = {c: days for c, days in by_day.items() if kinds[c] == 'exposure'}
    timelines = [Timeline(m) for m in facts['medications']]
    results = []

    def emit(method, exposure, outcome, strength, direction, effect, statement, supporting, contrary, competing, limitations, extra=''):
        results.append({'association_id': association_id(method, exposure, outcome, extra), 'method': method,
                        'exposure_concept': exposure, 'exposure_label': labels.get(exposure, exposure),
                        'outcome_concept': outcome, 'outcome_label': labels.get(outcome, outcome),
                        'strength': strength, 'direction': direction, 'effect': effect,
                        'statement': statement + ' ' + CAUTION, 'supporting_records': sorted(set(supporting)),
                        'contrary_records': sorted(set(contrary)), 'competing': competing, 'limitations': limitations,
                        'evidence_kind': 'derived_association'})

    for outcome, odays in sorted(outcomes.items()):
        observed = sorted(odays)
        present = [d for d in observed if odays[d]['state'] == 'present']
        absent = [d for d in observed if odays[d]['state'] == 'absent']
        if len(observed) < 3 or not present or not absent:
            continue
        candidates = []
        for exposure, edays in sorted(exposures.items()):
            candidates.append(('exposure', exposure, lambda d, edays=edays: edays[d]['state'] == 'present' if d in edays else None,
                               lambda d, edays=edays: edays.get(d, {}).get('records', []), edays))
        for t in timelines:
            candidates.append(('medication', t.med['concept'], lambda d, t=t: {'on': True, 'off': False}.get(t.state(d)),
                               lambda d, t=t: [e['rid'] for e in t.events if e['date'] <= d][-1:], t))
        evaluated = []
        for kind, exposure, state, records, source in candidates:
            best = None
            # A reported exposure may act the same day or the next; a medication's state is continuous.
            for lag in ((0, 1) if kind == 'exposure' else (0,)):
                cells = {'a': [], 'b': [], 'c': [], 'd': []}
                for d in observed:
                    exposed = state(d - timedelta(days=lag))
                    if exposed is None:
                        continue
                    sick = odays[d]['state'] == 'present'
                    cells['a' if exposed and sick else 'b' if exposed else 'c' if sick else 'd'].append(d)
                a, b, c, dd = (len(cells[k]) for k in 'abcd')
                if a + b + c + dd == 0:
                    continue
                result = {'lag_days': lag, 'cells': cells, 'counts': {'exposed_with': a, 'exposed_without': b, 'unexposed_with': c, 'unexposed_without': dd}}
                if a + b and c + dd:
                    result['risk_ratio'] = round(risk_ratio(a, b, c, dd), 2)
                    result['p_value'] = round(fisher_greater(a, b, c, dd), 4)
                if best is None or (result.get('risk_ratio', 0), a) > (best.get('risk_ratio', 0), best['counts']['exposed_with']):
                    best = result
            if best:
                evaluated.append((kind, exposure, state, records, source, best))
        summary = []
        for kind, exposure, state, records, source, best in evaluated:
            counts = best['counts']
            testable = 'risk_ratio' in best
            summary.append({'exposure_concept': exposure, 'exposure_label': labels.get(exposure, exposure), 'kind': kind,
                            'lag_days': best['lag_days'], **counts, 'risk_ratio': best.get('risk_ratio'), 'p_value': best.get('p_value'),
                            'testable': testable})
        summary.sort(key=lambda s: (-(s['risk_ratio'] or 0), s['exposure_label']))
        for rank, s in enumerate(summary, 1):
            s['rank'] = rank if s['testable'] else None
        for kind, exposure, state, records, source, best in evaluated:
            counts, cells = best['counts'], best['cells']
            a, b, c, dd = counts.values()
            others = [s for s in summary if s['exposure_concept'] != exposure]
            o_label, e_label = labels.get(outcome, outcome), labels.get(exposure, exposure)
            sym_records = lambda days: [r for d in days for r in odays[d]['records']]
            exp_records = lambda days: [r for d in days for r in records(d - timedelta(days=best['lag_days']))]
            limitations = []
            if len(absent) < 3:
                limitations.append(f'Only {len(absent)} symptom-free day(s) are recorded; days without a report are treated as unknown, not symptom-free.')
            if kind == 'exposure' and len(source) < 5:
                limitations.append(f'{e_label} is recorded on only {len(source)} day(s).')
            if 'risk_ratio' in best:
                if best['risk_ratio'] <= 1:
                    continue
                rr, p = best['risk_ratio'], best['p_value']
                strength = grade_contrast(a, b, c, dd, rr, p)
                when = 'while using' if kind == 'medication' else 'the same day as' if best['lag_days'] == 0 else 'the day after'
                effect = {**counts, 'risk_ratio': rr, 'p_value': p, 'lag_days': best['lag_days']}
                if kind == 'exposure' and any(source[d - timedelta(days=best['lag_days'])]['amount'] is not None for d in cells['a'] + cells['b'] + cells['c'] + cells['d']):
                    amounts = lambda days: [source[d - timedelta(days=best['lag_days'])]['amount'] for d in days
                                            if source[d - timedelta(days=best['lag_days'])]['amount'] is not None]
                    sick, well = amounts(cells['a'] + cells['c']), amounts(cells['b'] + cells['d'])
                    if sick and well:
                        effect['mean_amount_on_symptom_days'] = round(sum(sick) / len(sick), 1)
                        effect['mean_amount_on_symptom_free_days'] = round(sum(well) / len(well), 1)
                detail = (f' {o_label} was reported on {a} of {a + b} observed day(s) {when} {e_label}, '
                          f'and on {c} of {c + dd} observed day(s) without it.')
                stronger = [s for s in others if s['testable'] and (s['risk_ratio'] or 0) > rr]
                if not stronger and any(s['kind'] == 'medication' and not s['testable'] for s in others):
                    detail += ' It varies with the symptom where the constant background exposures cannot.'
                emit('exposure_episode_contrast', exposure, outcome, strength, 'positive', effect,
                     STATEMENTS[strength].format(e=e_label, o=o_label) + detail,
                     sym_records(cells['a'] + cells['d']) + exp_records(cells['a'] + cells['d']),
                     sym_records(cells['b'] + cells['c']) + exp_records(cells['b'] + cells['c']), others, limitations)
            elif kind == 'medication' and a + b and not c + dd and b:
                strength = 'strong' if b >= 4 else 'moderate' if b >= 2 else 'weak'
                emit('constant_background', exposure, outcome, strength, 'none',
                     {'background_state': 'present', **counts, 'lag_days': best['lag_days']},
                     f'{e_label} was in use on all {a + b} observed day(s), including {b} without {o_label}, so it cannot explain '
                     f'why {o_label} comes and goes. It may still affect susceptibility.',
                     sym_records(cells['b']), sym_records(cells['a']), others, limitations)
            elif kind == 'medication' and c + dd and not a + b and c:
                change = source.last_change_before(observed[0])
                if not change or (observed[0] - change['date']).days > RECENT_STOP_DAYS:
                    continue
                gap = (present[0] - change['date']).days
                strength = 'strong' if c >= 4 else 'moderate' if c >= 2 else 'weak'
                emit('constant_background', exposure, outcome, strength, 'none',
                     {'background_state': 'absent', **counts, 'stopped_on': change['date'].isoformat(), 'days_from_stop_to_first_symptom': gap},
                     f'{e_label} was not in use on any observed day; it was {change["change_type"] or "stopped"} on {change["date"]:%B} {change["date"].day}, '
                     f'yet {o_label} was reported on {c} day(s) afterwards, the first {gap} day(s) later. Current use cannot explain those episodes; '
                     'lingering effects after stopping are not ruled out by these records.',
                     sym_records(cells['c']) + [change['rid']], [], others, limitations)

    for t in timelines:
        changes = [e for e in t.events if e['status']]
        if not changes:
            continue
        start = changes[0]['date'] - timedelta(days=SERIES_LOOKBACK_DAYS)
        e_label = labels.get(t.med['concept'], t.med['concept'])
        for outcome, s in sorted(facts['series'].items()):
            points = sorted((d, v, rid) for d, v, rid in s['points'] if d >= start)
            if len(points) < 4:
                continue
            segments = []
            for d, v, rid in points:
                state = t.state(d)
                if state is None:
                    continue
                if segments and segments[-1]['state'] == state:
                    segments[-1]['points'].append((d, v, rid))
                else:
                    segments.append({'state': state, 'points': [(d, v, rid)]})
            for seg in segments:
                seg['slope_per_week'] = slope_per_week([(d, v) for d, v, _ in seg['points']])
                seg['from'], seg['to'] = seg['points'][0][0].isoformat(), seg['points'][-1][0].isoformat()
            on = [g['slope_per_week'] for g in segments if g['state'] == 'on' and g['slope_per_week'] is not None]
            off = [g['slope_per_week'] for g in segments if g['state'] == 'off' and g['slope_per_week'] is not None]
            o_label, unit = s['label'], s.get('unit') or ''
            last = changes[-1]
            before = [p for p in points if last['date'] - timedelta(days=BASELINE_DAYS) <= p[0] <= last['date']]
            after = [p for p in points if p[0] > last['date']]
            effect = {'segments': [{k: g[k] for k in ('state', 'from', 'to', 'slope_per_week')} | {'points': len(g['points'])} for g in segments],
                      'unit': unit}
            limitations = []
            competing = []
            for other in timelines:
                if other is t:
                    continue
                near = [e for e in other.events if e['status'] and any(abs((e['date'] - c['date']).days) <= 14 for c in changes)]
                competing += [{'exposure_concept': other.med['concept'], 'exposure_label': labels.get(other.med['concept'], other.med['concept']),
                               'change_type': e['change_type'], 'date': e['date'].isoformat()} for e in near]
            if competing:
                limitations.append('Other medications changed within two weeks of these changes, so their effects are not separable here.')
            if on and off:
                mean_on, mean_off = sum(on) / len(on), sum(off) / len(off)
                differ = abs(mean_on - mean_off)
                if differ < 0.25:
                    continue
                direction = 'decrease' if mean_on < mean_off else 'increase'
                alternating = len(segments) >= 3 and all(segments[i]['state'] != segments[i + 1]['state'] for i in range(len(segments) - 1))
                consistent = all((g < min(off)) if direction == 'decrease' else (g > max(off)) for g in on)
                strength = 'strong' if alternating and consistent and len(on) + len(off) >= 3 else 'moderate' if consistent else 'weak'
                effect.update(mean_slope_on=round(mean_on, 3), mean_slope_off=round(mean_off, 3))
                detail = (f' {o_label} changed by {mean_on:+.1f} {unit}/week on average while on {e_label} and {mean_off:+.1f} {unit}/week while off'
                          + (', and the pattern reversed when it stopped and resumed when it restarted.' if strength == 'strong' else '.'))
                verb = 'lower' if direction == 'decrease' else 'higher'
                emit('exposure_series_change', t.med['concept'], outcome, strength, direction, effect,
                     STATEMENTS[strength].format(e=e_label, o=f'{verb} {o_label}') + detail,
                     [rid for g in segments for *_, rid in g['points']] + [e['rid'] for e in changes], [], competing, limitations)
            elif before and after and t.state(after[0][0]) == 'on':
                change = after[-1][1] - before[-1][1]
                if abs(change) < 0.5:
                    continue
                direction = 'decrease' if change < 0 else 'increase'
                days = (after[-1][0] - last['date']).days
                limitations.append('Only one change has measurements on both sides; no off-period comparison yet.')
                if len(after) < 2:
                    limitations.append('Only one measurement since the change.')
                effect.update(baseline=before[-1][1], baseline_date=before[-1][0].isoformat(), latest=after[-1][1],
                              latest_date=after[-1][0].isoformat(), change=round(change, 2))
                verb = 'lower' if direction == 'decrease' else 'higher'
                if days < FOLLOW_UP_DAYS:
                    limitations.append(f'Measured for only {days} day(s) since the change; short-term fluctuation can dominate.')
                strength = 'weak' if len(after) >= 2 and days >= FOLLOW_UP_DAYS else 'insufficient'
                emit('exposure_series_change', t.med['concept'], outcome, strength, direction, effect,
                     STATEMENTS[strength].format(e=e_label, o=f'{verb} {o_label}') +
                     f' {o_label} went from {before[-1][1]:g} to {after[-1][1]:g} {unit} in the {days} day(s) since {e_label} was '
                     f'{last["change_type"] or "started"} on {last["date"]:%B} {last["date"].day}.',
                     [before[-1][2]] + [rid for *_, rid in after] + [last['rid']], [], competing, limitations, extra='single_change')
    return results


def check_in_plan(facts, associations, today, window=14):
    """What a daily check-in should ask about: symptoms and exposures in open associations, and the recent days with no report."""
    days = [today - timedelta(days=i) for i in range(window)]
    recorded = daily(facts['observations'])
    kinds = {o['concept']: o['kind'] for o in facts['observations']}
    items = {}
    for a in associations:
        if a['strength'] == 'strong' and a['method'] != 'exposure_episode_contrast':
            continue
        for concept, label in ((a['outcome_concept'], a['outcome_label']), (a['exposure_concept'], a['exposure_label'])):
            if concept in recorded:
                item = items.setdefault(concept, {'concept': concept, 'label': label, 'associations': set()})
                item['associations'].add(a['association_id'])
    plan = []
    for concept, item in sorted(items.items()):
        missing = [d.isoformat() for d in days if d not in recorded[concept]]
        plan.append({'concept': concept, 'label': item['label'], 'unreported_days': missing,
                     'reported_days': window - len(missing), 'association_count': len(item['associations']),
                     'kind': kinds[concept],
                     'ask': (f"Any {item['label'].lower()} today, and how much?" if kinds[concept] == 'exposure'
                             else f"Did you have any {item['label'].lower()} today?") + ' Record "none" too, as an explicit absence.'})
    return sorted(plan, key=lambda p: (-p['association_count'], -len(p['unreported_days']), p['label']))


def pooled_patterns(rows, minimum):
    """Aggregate opted-in patients' current associations into cross-patient patterns.

    `rows` are (patient_id, method, exposure_pool_key, exposure_label,
    outcome_pool_key, outcome_label, direction, strength, effect). A pattern is
    kept only when at least `minimum` distinct patients show it. The output
    carries no patient, record, date or free-text field."""
    groups = {}
    for patient, method, ekey, elabel, okey, olabel, direction, strength, effect in rows:
        if not ekey or not okey or strength == 'insufficient':
            continue
        g = groups.setdefault((method, ekey, okey, direction), {'patients': {}, 'elabels': {}, 'olabels': {}})
        rank = STRENGTHS.index(strength)
        if rank >= STRENGTHS.index(g['patients'].get(patient, ('insufficient', None))[0]):
            g['patients'][patient] = (strength, effect)
        g['elabels'][elabel] = g['elabels'].get(elabel, 0) + 1
        g['olabels'][olabel] = g['olabels'].get(olabel, 0) + 1
    patterns = []
    for (method, ekey, okey, direction), g in sorted(groups.items()):
        n = len(g['patients'])
        if n < minimum:
            continue
        strengths = [s for s, _ in g['patients'].values()]
        measure = 'risk_ratio' if method == 'exposure_episode_contrast' else 'mean_slope_on' if method == 'exposure_series_change' else None
        values = sorted(e[measure] for _, e in g['patients'].values() if measure and e.get(measure) is not None)
        median = values[len(values) // 2] if len(values) % 2 else (values[len(values) // 2 - 1] + values[len(values) // 2]) / 2 if values else None
        common = lambda labels: sorted(labels.items(), key=lambda item: (-item[1], item[0]))[0][0]
        patterns.append({'pattern_id': association_id(method, ekey, okey, direction).replace('assoc_', 'pattern_'), 'method': method,
                         'exposure_key': ekey, 'exposure_label': common(g['elabels']), 'outcome_key': okey, 'outcome_label': common(g['olabels']),
                         'direction': direction, 'contributors': n,
                         'share_moderate_or_strong': round(sum(s in ('moderate', 'strong') for s in strengths) / n, 1),
                         'median_effect': {'measure': measure, 'value': round(median, 1)} if median is not None else None})
    return patterns


def as_date(value):
    return value if isinstance(value, date) or value is None else date.fromisoformat(str(value)[:10])


def load_facts(conn, patient, resource_ids):
    """The dated observations, medication timelines and measurement series of the given current records."""
    rows = conn.execute("""SELECT x.resource_id, x.category, x.name, x.clinical_date, x.assertion, x.numeric_value,
          coalesce(x.unit_ucum, x.unit_source) AS unit, x.concept_id, x.normalized->'source_details' AS details,
          c.label AS concept_label, c.basis, c.family
        FROM health.resources x LEFT JOIN health.concepts c ON c.patient_id = x.patient_id AND c.concept_id = x.concept_id
        WHERE x.patient_id = %s AND x.resource_id = ANY(%s) ORDER BY x.resource_id""", (patient, list(resource_ids))).fetchall()

    def label(r):
        # A key concept is named by its key, not by one report's sentence.
        if r['basis'] == 'source_key':
            return r['concept_id'].rsplit(':', 1)[1].replace('_', ' ')
        return r['concept_label'] or r['name']

    concepts = {r['concept_id']: (r['basis'], r['family'], label(r)) for r in rows if r['concept_id']}
    observations = []
    for r in rows:
        if (r['category'] in ('symptom_observation', 'reported_exposure') and r['clinical_date'] and r['concept_id']
                and r['assertion'] in ('present', 'absent')):
            amount = ((r['details'] or {}).get('details') or {}).get('amount') or {}
            observations.append({'rid': r['resource_id'], 'concept': r['concept_id'], 'label': label(r), 'family': r['family'],
                                 'kind': 'symptom' if r['category'] == 'symptom_observation' else 'exposure',
                                 'date': r['clinical_date'], 'assertion': r['assertion'],
                                 'amount': amount.get('value') if isinstance(amount.get('value'), (int, float)) else None})
    medications = {}
    for r in rows:
        d = r['details'] or {}
        if r['category'] == 'medication_regimen' and r['concept_id'] and d.get('medication_id'):
            started = ((d.get('known_started_on') or {}).get('date') or {}).get('date')
            medications[d['medication_id']] = {'concept': r['concept_id'], 'label': label(r), 'status': d.get('status'),
                                               'known_since': as_date(started or r['clinical_date']), 'events': []}
    for r in rows:
        d = r['details'] or {}
        status = CHANGE_STATUS.get(d.get('change_type'))
        if r['category'] == 'medication_change' and d.get('medication_id') in medications and r['clinical_date'] and status:
            medications[d['medication_id']]['events'].append({
                'date': r['clinical_date'], 'status': status, 'rid': r['resource_id'],
                'previous': (d.get('previous') or {}).get('status'),
                'change_type': 'changed' if d['change_type'] == 'regimen_changed' else d['change_type']})
    series = {}
    for r in rows:
        if r['family'] == 'measurement' and r['numeric_value'] is not None and r['clinical_date'] and r['concept_id']:
            entry = series.setdefault(r['concept_id'], {'label': label(r), 'points': {}})
            entry['points'].setdefault(r['unit'], []).append((r['clinical_date'], r['numeric_value'], r['resource_id']))
    for s in series.values():
        # One unit per series, the most common; mixed units are never converted here.
        s['unit'], s['points'] = max(s['points'].items(), key=lambda item: (len(item[1]), str(item[0])))
    return {'observations': observations, 'medications': list(medications.values()), 'series': series, 'concepts': concepts}
