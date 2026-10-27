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
import collections
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
# Derived exposures. Seven hours is the adult minimum the American Academy of
# Sleep Medicine recommends; a night under it counts as short sleep.
WORKOUT = 'concept:derived:workout'
SHORT_SLEEP = 'concept:derived:short_sleep'
SHORT_SLEEP_HOURS = 7
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


def pool_key(concept_id, basis, family, label, generic=None):
    """A concept identity shared across patients, or None when the concept is only meaningful to one archive.

    Curated, coded and structured-key concepts are shared vocabularies. Medications
    pool by generic name, so linaclotide is one medication whatever brand a record
    names. Without a known generic they pool by the label without brand, form or
    dose, because curated concepts belong to one archive and codes miss self-reports."""
    if family != 'medication' and basis in ('curated_registry', 'source_code', 'source_key', 'engine_derived'):
        return concept_id
    if family == 'medication' and generic:
        return f'medication:{generic}'
    if family == 'medication' and label:
        # Strength and form start at the first word that begins with a digit; TB-500 keeps its number.
        name = re.split(r'\s\d', label.casefold().split('(')[0])[0]
        name = ' '.join(re.sub(r'[^a-z0-9 -]', ' ', name).split()).strip(' -')
        return f'medication:{name}' if name else None
    return None


def brand_keys(brands):
    """Pooling keys for the brands a patient's medication records name, pooled apart from the generic."""
    return [f'medication-brand:{b}' for b in sorted(brands or ())]


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
    """Per concept and day: present if any record that day says present, else absent; the records behind each.

    A day's amount is the largest reported, or the sum for observations marked
    `sum_amounts`, such as two workouts in one day."""
    days = {}
    for o in observations:
        key = (o['concept'], o['date'])
        entry = days.setdefault(key, {'present': False, 'absent': False, 'records': [], 'amount': None, 'unit': o.get('amount_unit')})
        entry[o['assertion']] = True
        entry['records'].append(o['rid'])
        if o.get('amount') is not None:
            entry['amount'] = (entry['amount'] or 0) + o['amount'] if o.get('sum_amounts') else max(entry['amount'] or 0, o['amount'])
    result = {}
    for (concept, day), e in days.items():
        state = 'present' if e['present'] else 'absent'
        amount = e['amount'] if e['amount'] is not None else (0 if state == 'absent' else None)
        result.setdefault(concept, {})[day] = {'state': state, 'records': sorted(e['records']), 'amount': amount, 'unit': e['unit']}
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
    value, rid)]}}) and `concepts` ({concept_id: (basis, family, label, generic, brands)}).
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
                        effect['amount_unit'] = next((v['unit'] for v in source.values() if v['unit']), None)
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


RED_FLAGS = ['chest pain or pressure', 'trouble breathing', 'fainting or nearly fainting', 'black or tarry stool, or vomiting blood',
             'severe belly pain', 'swelling of the lips, tongue or throat', 'sudden weakness, confusion or trouble speaking',
             'thoughts of harming yourself']
# Active symptoms that also belong in the urgent screen, by concept or label.
RED_FLAG_PATTERN = re.compile(r'(black|tarry|dark)[ _]stool|blood|chest[ _]pain|short(ness)?[ _]of[ _]breath|faint', re.I)
STOOL_PATTERN = re.compile(r'stool|constipat|diarrh|bowel|rectum', re.I)
# Symptoms of one body system are asked as one question, so one bowel movement is described once.
SYMPTOM_GROUPS = [('bowel', re.compile(r'stool|constipat|diarrh|bowel|rectum|bloat|trapped[ _]gas|flatul', re.I)),
                  ('urinary', re.compile(r'urin|pee|bladder', re.I))]
# Things the patient sees rather than feels: asked as yes or no and when, never on a 0 to 10 scale.
OBSERVED_PATTERN = re.compile(r'stool|urine|diarrh|colou?r|dark|ribbon|shape|blood|rash|lips', re.I)


# A tracked measurement key that names a reading of several concepts.
MEASUREMENT_KEYS = {'blood_pressure': ['concept:systolic_blood_pressure', 'concept:diastolic_blood_pressure']}
SETTINGS = {'mode': 'one_at_a_time', 'check_in_time': 'morning'}
REVIEW_DAYS = 30


def tracking_plan(tracking):
    """The current check-in plan: the latest agreed action per item kind and key."""
    plan = {}
    for t in sorted(tracking, key=lambda t: (t['reported_at'], t['rid'])):
        plan[(t['kind'], t['key'])] = t
    return plan


def keyed_concepts(facts):
    """The concept each structured symptom or exposure key joined, from the patient's reports."""
    return {(r['kind'], r['key']): r['concept'] for r in facts.get('reports', []) if r.get('key')}


def tracked_concepts(plan, keyed, kind, action):
    concepts = {}
    for (k, key), t in plan.items():
        if k == kind and t['action'] == action:
            concepts[keyed.get((kind, key), f'concept:key:{kind}:{key}')] = t
    return concepts


def check_in_plan(facts, associations, today, window=14, active_days=60):
    """The morning check-in agenda, in order, built from the patient's records.

    Steps: urgent screen, open question, tracked measurements, sleep, yesterday's
    workout, medication changes and as-needed doses, exposures, each symptom,
    an offer to fill in missed days, and the read-back. Symptoms come from open
    associations and from active symptoms: present when last reported, within
    `active_days`, and either described as ongoing (current, recurring or a
    treatment response, not tied to one day) or reported at the last check-in.
    A one-off report about an earlier day and a lifelong trait are not active.

    The tracking plan agreed at intake and since adds measurements, exposures,
    symptoms and tailored questions, and removes items the patient chose to
    stop. Without a plan, `intake_needed` is true."""
    recorded = daily(facts['observations'])
    plan = tracking_plan(facts.get('tracking', []))
    keyed = keyed_concepts(facts)
    stopped_symptoms = tracked_concepts(plan, keyed, 'symptom', 'stop')
    stopped_exposures = tracked_concepts(plan, keyed, 'exposure', 'stop')
    since = facts.get('last_reported')
    since = since if since and since < today else None
    since_text = f'{since:%A}, {since:%B} {since.day}' if since else 'your last check-in'
    symptoms, exposures = {}, {}
    for a in associations:
        if a['strength'] == 'strong' and a['method'] != 'exposure_episode_contrast':
            continue
        for concept, label in ((a['outcome_concept'], a['outcome_label']), (a['exposure_concept'], a['exposure_label'])):
            if concept not in recorded:
                continue
            kind = next(o['kind'] for o in facts['observations'] if o['concept'] == concept)
            target = symptoms if kind == 'symptom' else exposures
            item = target.setdefault(concept, {'label': label, 'associations': set(), 'reasons': set()})
            item['associations'].add(a['association_id'])
            item['reasons'].add('association')
    def last_state(concept):
        # The most recent dated observation or report of the symptom, whichever is later.
        dated = [(d, v['state']) for d, v in recorded.get(concept, {}).items()]
        reported = [(r['reported'], r['assertion']) for r in facts.get('reports', []) if r['concept'] == concept and r['assertion'] in ('present', 'absent')]
        return max(dated + reported, default=(None, None), key=lambda x: (x[0], x[1] == 'present'))

    # Active: present at the latest report, within active_days, and either described as ongoing
    # (not tied to one day) or reported at the last check-in, so the nurse asks whether it is still there.
    by_concept = {}
    for r in facts.get('reports', []):
        if r['kind'] == 'symptom' and r['assertion'] in ('present', 'absent'):
            by_concept.setdefault(r['concept'], []).append(r)
    for concept, reports in by_concept.items():
        day, state = last_state(concept)
        ongoing = any(not r.get('dated') and r.get('scope') in (None, 'current_state', 'recurring_pattern', 'treatment_response')
                      and r['assertion'] == 'present' for r in reports)
        at_last_check_in = since is not None and day == since
        if state == 'present' and (today - day).days <= active_days and (ongoing or at_last_check_in):
            item = symptoms.setdefault(concept, {'label': reports[-1]['label'], 'associations': set(), 'reasons': set()})
            item['reasons'].add('active_symptom')

    for concept, t in tracked_concepts(plan, keyed, 'symptom', 'track').items():
        item = symptoms.setdefault(concept, {'label': t['label'], 'associations': set(), 'reasons': set()})
        item['reasons'].add('tracking_plan')
        item['question'] = t['question']
    for concept in stopped_symptoms:
        symptoms.pop(concept, None)

    symptom_items = []
    for concept, item in symptoms.items():
        day, state = last_state(concept)
        label = item['label']
        text = concept + ' ' + label
        observed = bool(OBSERVED_PATTERN.search(text))
        symptom_items.append({
            'concept': concept, 'label': label, 'reasons': sorted(item['reasons']), 'association_count': len(item['associations']),
            'last_state': state, 'last_state_date': day.isoformat() if day else None,
            'group': next((g for g, pattern in SYMPTOM_GROUPS if pattern.search(text)), None),
            # A red-flag symptom is asked once, in the urgent screen.
            'asked_in_urgent_screen': bool(RED_FLAG_PATTERN.search(text)),
            'severity': not observed,
            'ask': item.get('question') or f'Do you still have {label.lower()}?',
            'if_yes': 'Ask when it last happened (last night, this morning, all day).' if observed else
                      'Ask when (last night, this morning, all day), severity 0 to 10, and better, same or worse than last time.',
            'if_no': 'Ask when it stopped, and record it as absent from that day.' if state == 'present'
                     else 'Record it as absent today.'})
    symptom_items.sort(key=lambda i: i['label'])
    symptom_items.sort(key=lambda i: i['last_state_date'] or '', reverse=True)
    symptom_items.sort(key=lambda i: i['association_count'], reverse=True)
    for i in symptom_items:
        if i['asked_in_urgent_screen']:
            i['group'] = None
    sizes = collections.Counter(i['group'] for i in symptom_items if i['group'])
    for i in symptom_items:
        if sizes[i['group']] < 2:
            i['group'] = None
    groups = []
    for key, _ in SYMPTOM_GROUPS:
        members = [i for i in symptom_items if i['group'] == key]
        if not members:
            continue
        names = ', '.join(i['label'].lower() for i in members)
        bristol = any(STOOL_PATTERN.search(i['concept'] + ' ' + i['label']) for i in members)
        ask = (f'How have your bowel movements been since {since_text}? How many, and which Bristol type (1 to 7)? Any of these: {names}?'
               if key == 'bowel' else f'Any of these since {since_text}: {names}?')
        felt = [i['label'].lower() for i in members if i['severity']]
        stopped = [i['label'].lower() for i in members if i['last_state'] == 'present']
        groups.append({'key': key, 'ask': ask, 'items': [i['concept'] for i in members], 'bristol': bristol,
                       'follow_up': ' '.join(filter(None, [
                           f"For any of {', '.join(felt)} they have: severity 0 to 10, and better, same or worse." if felt else '',
                           f"For any of {', '.join(stopped)} that is gone: when it stopped." if stopped else '',
                           'Record each listed item as present or absent; ask only what the answer left open.']))})

    # A reported red-flag symptom is folded into the standard item it overlaps, not asked beside it.
    urgent = list(RED_FLAGS)
    for i in symptom_items:
        if not i['asked_in_urgent_screen']:
            continue
        note = f"you reported {i['label'].lower()} on {i['last_state_date']}; if it has stopped, when?"
        words = set(re.findall(r'stool|blood|chest|breath|faint', i['concept'] + ' ' + i['label'], re.I))
        n = next((n for n, flag in enumerate(urgent) if words & set(re.findall(r'stool|blood|chest|breath|faint', flag))), None)
        if n is None:
            urgent.append(f"{i['label']} ({note})")
        else:
            urgent[n] += f' ({note})'
    order = ['concept:body_weight', 'concept:systolic_blood_pressure', 'concept:diastolic_blood_pressure', 'concept:heart_rate']
    measured = sorted(facts.get('reported_measurements', {}).items(), key=lambda m: (order.index(m[0]) if m[0] in order else len(order), m[1]['label']))
    tracked_measurements, stopped_measurements = {}, set()
    for (kind, key), t in plan.items():
        if kind == 'measurement':
            for c in MEASUREMENT_KEYS.get(key, ['concept:' + key]):
                if t['action'] == 'track':
                    tracked_measurements[c] = t
                else:
                    stopped_measurements.add(c)
    measurements = [{'concept': c, 'label': m['label'], 'unit': m['unit'], 'last_reported': m['last'].isoformat()}
                    for c, m in measured if c not in stopped_measurements and ((today - m['last']).days <= 30 or c in tracked_measurements)]
    reported = facts.get('reported_measurements', {})
    measurements += [{'concept': c, 'label': t['label'], 'unit': None, 'last_reported': None}
                     for c, t in tracked_measurements.items() if c not in reported]
    measurements.sort(key=lambda m: order.index(m['concept']) if m['concept'] in order else len(order))
    drinks = any(o['concept'] == 'concept:exposure:alcohol' and o['assertion'] == 'present' and (today - o['date']).days <= 90
                 for o in facts['observations'])
    for concept, t in tracked_concepts(plan, keyed, 'exposure', 'track').items():
        exposures.setdefault(concept, {'label': t['label'], 'associations': set(), 'reasons': {'tracking_plan'}, 'question': t['question']})
    exposure_items = []
    alcohol_tracked = ('exposure', 'alcohol') in plan and plan[('exposure', 'alcohol')]['action'] == 'track'
    if (drinks or alcohol_tracked or 'concept:exposure:alcohol' in exposures) and 'concept:exposure:alcohol' not in stopped_exposures \
            and plan.get(('exposure', 'alcohol'), {}).get('action') != 'stop':
        exposure_items.append({'concept': 'concept:exposure:alcohol', 'label': 'Alcohol intake',
                               'ask': 'How many drinks did you have yesterday, and roughly when?', 'record_as': 'reported_exposure with a drink-count amount; 0 drinks is absent'})
    for concept, item in sorted(exposures.items()):
        if concept.startswith(('concept:derived:', 'concept:medication:')) or concept in ('concept:exposure:alcohol', 'concept:key:exposure:alcohol') \
                or concept in stopped_exposures or any(m['concept'] == concept for m in facts['medications']):
            continue
        exposure_items.append({'concept': concept, 'label': item['label'],
                               'ask': item.get('question') or f"Any {item['label'].lower()} yesterday, and how much?",
                               'record_as': 'reported_exposure; none is absent'})

    days_seen = {o['date'] for o in facts['observations']} | {d for m in facts.get('reported_measurements', {}).values() for d in m['dates']}
    # Days before the patient's first report or plan are not missed; a new patient has none.
    started = min(days_seen | {t['reported'] for t in facts.get('tracking', []) if t['reported']}, default=today)
    first = max(today - timedelta(days=window), since or today - timedelta(days=window), started)
    missed = [(first + timedelta(days=i)).isoformat() for i in range((today - first).days)
              if first + timedelta(days=i) not in days_seen and first + timedelta(days=i) != today]

    tailored = [{'key': key, 'label': t['label'], 'ask': t['question']} for (kind, key), t in sorted(plan.items())
                if kind == 'question' and t['action'] == 'track']
    preferences = {**SETTINGS, **{key: t['value'] for (kind, key), t in plan.items() if kind == 'setting' and t['action'] == 'track'}}
    review = review_items(facts, plan, symptom_items, today)

    steps = [
        {'key': 'opening', 'say': f"Morning check-in for {today:%A}, {today:%B} {today.day}." + (f' Your last check-in was {since_text}, so this covers since then.' if since else '')},
        {'key': 'urgent', 'ask': 'Before we start: since ' + since_text + ', have you had any of these?', 'items': urgent,
         'on_yes': 'Stop the check-in. Advise urgent care or emergency services as appropriate, and record what was reported.'},
        {'key': 'open', 'ask': f'Anything new or different since {since_text}, good or bad?',
         'record_as': 'whatever fits: symptom_observation, reported_exposure, patient_context'},
        {'key': 'measurements', 'ask': ('What were your ' if ' and ' in measurement_names(measurements) else 'What was your ')
                                       + measurement_names(measurements) + ' this morning?',
         'items': measurements, 'record_as': 'anthropometric_measurement, vital_sign_measurement (blood pressure components paired)'} if measurements else None,
        {'key': 'sleep', 'ask': 'How did you sleep last night? When did you go to bed and get up, and did you wake up during the night?',
         'record_as': 'sleep_observation dated today'},
        {'key': 'workout', 'ask': 'Did you work out yesterday? If so, describe it, for example 40 minutes of cardio twice, or 90 minutes of weight training.',
         'record_as': 'exercise_session dated yesterday, one per session; status none_reported for no workout'},
        {'key': 'medications', 'asks': [f'Have you added, changed or stopped any medications since {since_text}?',
                                        f'Have you taken any as-needed medications since {since_text}?'],
         'record_as': 'record_medication: medication_change for changes, medication_administration for as-needed doses'},
        {'key': 'exposures', 'items': exposure_items} if exposure_items else None,
        {'key': 'symptoms', 'groups': groups, 'items': symptom_items,
         'scales': {'severity': '0 to 10, 0 none and 10 worst imaginable', 'stool': 'Bristol type 1 to 7'},
         'record_as': 'symptom_observation per symptom with occurrence date, numeric_severity for felt symptoms and, for stool, stool_form'} if symptom_items else None,
        {'key': 'tailored', 'items': tailored, 'record_as': 'whatever fits the answer: symptom_observation, reported_exposure, patient_context'} if tailored else None,
        {'key': 'missed_days', 'days': missed,
         'ask': 'I have no check-in for ' + ', '.join(missed) + '. Do you want to fill any of those in? It is fine if you do not remember.',
         'on_yes': 'Ask only about the days and items they can recall; note in source_context that the answers are recalled.'} if missed else None,
        {'key': 'review', 'items': review,
         'ask': 'A quick review of what I ask each day.',
         'on_answer': 'Record each agreed change as a check_in_tracking record. Change nothing they did not agree to.'} if review else None,
        {'key': 'close', 'say': 'Read back everything you will record, confirm it, record it, then call associations and share what changed in a line or two, plus any safety note.'},
    ]
    steps = [dict(step=n, **s) for n, s in enumerate([s for s in steps if s], 1)]
    return {'date': today.isoformat(), 'covers_since': since.isoformat() if since else None, 'intake_needed': not plan,
            'preferences': preferences, 'steps': steps, 'questionnaire': questionnaire(steps)}


def review_items(facts, plan, symptom_items, today):
    """Questions about the plan itself, at most once every REVIEW_DAYS after the last plan change."""
    changed = max((t['reported'] for t in plan.values() if t['reported']), default=None)
    if changed is None or (today - changed).days < REVIEW_DAYS:
        return []
    items = []
    for i in symptom_items:
        if 'tracking_plan' in i['reasons'] and i['last_state'] == 'absent' and i['last_state_date'] \
                and (today - date.fromisoformat(i['last_state_date'])).days >= REVIEW_DAYS:
            items.append({'concept': i['concept'], 'ask': f"You haven't had {i['label'].lower()} since at least {i['last_state_date']}. Should I stop asking about it?"})
    for m in facts['medications']:
        started = [e['date'] for e in m['events'] if e['status'] == 'active'] + ([m['known_since']] if m['known_since'] else [])
        if any(d and d > changed for d in started):
            items.append({'concept': m['concept'], 'ask': f"You started {m['label']} recently. Is there anything you want me to ask about each day while you take it?"})
    return items


EQUIPMENT = {'bathroom scale': 'body_weight', 'blood pressure cuff': 'blood_pressure', 'pulse (from a cuff, watch or fingertip)': 'heart_rate',
             'glucose meter or continuous glucose monitor': 'blood_glucose', 'thermometer': 'body_temperature', 'pulse oximeter': 'oxygen_saturation'}
HABITS = ['alcohol', 'caffeine', 'nicotine or tobacco', 'cannabis', 'diet pattern', 'usual sleep', 'usual exercise']


def current_status(medication):
    """A regimen's status after its latest reported change; the regimen record keeps the status it was reported with."""
    events = sorted(medication['events'], key=lambda e: e['date'])
    return events[-1]['status'] if events else medication['status']


def intake_plan(facts, associations, today):
    """The first-visit intake, or a catch-up for an archive that already holds records.

    Each section lists what the archive already knows, so only the gaps are asked.
    The intake ends with an agreed tracking plan: the measurements, exposures,
    symptoms and tailored questions the daily check-in will ask, recorded as
    check_in_tracking records."""
    current = check_in_plan(facts, associations, today)
    steps = {s['key']: s for s in current['steps']}
    symptoms = [i['label'] for i in steps.get('symptoms', {}).get('items', [])]
    medications = sorted({m['label'] for m in facts['medications'] if current_status(m) == 'active'})
    habits = sorted({r['label'] for r in facts.get('reports', []) if r['kind'] == 'exposure'})
    recent = [{'concept': c, 'label': m['label']} for c, m in facts.get('reported_measurements', {}).items() if (today - m['last']).days <= 90]
    measured = measurement_names(recent).replace(' and ', ', ').split(', ') if recent else []
    sections = [
        {'key': 'goals', 'ask': 'What do you most want to figure out or keep an eye on?',
         'why': 'Goals decide which connections to look for and which tailored questions to propose.', 'known': []},
        {'key': 'symptoms', 'ask': 'What is bothering you now? For each one: since when, how often, how bad from 0 to 10, and what seems to set it off or ease it.',
         'known': symptoms, 'record_as': 'symptom_observation (current_state), recurring_pattern, patient_hypothesis for suspected triggers'},
        {'key': 'medications', 'ask': 'Everything you take, including supplements, peptides and injectables: dose, schedule and when you started.',
         'known': medications, 'record_as': 'record_medication regimens'},
        {'key': 'history', 'ask': 'Any diagnosed conditions, past surgeries, allergies, and conditions that run in your family?',
         'known': [], 'record_as': 'patient_context', 'note': 'Check profile and the clinical records first; ask only what they do not cover.'},
        {'key': 'habits', 'ask': 'Tell me about ' + ', '.join(HABITS) + '.', 'known': habits,
         'record_as': 'reported_exposure with amounts and schedule, exercise_routine'},
        {'key': 'equipment', 'ask': 'Which of these do you have at home: ' + ', '.join(EQUIPMENT) + '?', 'known': measured,
         'why': 'Each one becomes a daily measurement question.'},
        {'key': 'basics', 'ask': 'Your age, sex and height.', 'known': [], 'record_as': 'patient_demographics, anthropometric_measurement',
         'note': 'Check profile first.'},
        {'key': 'preferences', 'ask': 'Do you want to check in in the morning or the evening, and get questions one at a time or all at once as a numbered list?',
         'known': [], 'record_as': 'check_in_tracking settings: mode, check_in_time'},
        {'key': 'tracking_plan', 'ask': 'Propose the daily check-in and ask them to approve or change it.',
         'propose': ['The standard core, always asked: urgent screen, anything new, sleep, yesterday\'s workout, medication changes and as-needed doses.',
                     'One measurement item for each piece of equipment they have.',
                     'One exposure item for each habit they report, such as drinks or caffeine.',
                     'Each current symptom.',
                     'At most three tailored questions tied to their goals, medications or suspected triggers. An example is appetite and nausea for someone starting a GLP-1 agonist. Say why each is proposed.'],
         'record_as': 'one check_in_tracking record per approved item, proposed_by assistant_approved_by_patient unless they asked for it themselves'},
    ]
    confirm = {'symptoms': 'I have these as current: {}. Which are still going on, and is anything else bothering you? For anything new: since when, how often, how bad from 0 to 10, and what seems to set it off.',
               'medications': 'I have you taking: {}. Is anything missing, stopped or changed, including supplements, peptides and injectables?',
               'habits': 'I have reports about: {}. Tell me about anything of ' + ', '.join(HABITS) + ' that is missing or has changed.',
               'equipment': 'You have reported your {} recently. Which of these do you have at home: ' + ', '.join(EQUIPMENT) + '?'}
    for section in sections:
        if section.get('known') and section['key'] in confirm:
            section['ask'] = confirm[section['key']].format(', '.join(section['known']))
    return {'date': today.isoformat(), 'catch_up': bool(facts.get('reports') or facts['medications'] or facts.get('reported_measurements')),
            'sections': sections, 'equipment_measurements': EQUIPMENT,
            'questionnaire': [f"{n}. {s['ask']}" for n, s in enumerate([s for s in sections if s['key'] != 'tracking_plan'], 1)]}


def measurement_names(measurements):
    """Plain names for the measurements to ask, with blood pressure asked as one reading."""
    plain = {'concept:body_weight': 'weight', 'concept:heart_rate': 'pulse'}
    names = []
    for m in measurements:
        name = 'blood pressure' if re.search(r'systolic|diastolic', m['concept'] + ' ' + m['label'], re.I) \
            else plain.get(m['concept'], m['label'].lower().replace('_', ' '))
        if name not in names:
            names.append(name)
    return ', '.join(names[:-1]) + ' and ' + names[-1] if len(names) > 1 else names[0]


def questionnaire(steps):
    """Every question of the agenda as one numbered list, for answering all at once."""
    questions = []
    for s in steps:
        if s['key'] == 'urgent':
            questions.append(s['ask'] + ' ' + '; '.join(s['items']) + '. If yes to any, say which and stop there.')
        elif s['key'] in ('open', 'measurements', 'sleep', 'workout', 'missed_days'):
            questions.append(s['ask'])
        elif s['key'] == 'medications':
            questions += s['asks']
        elif s['key'] in ('exposures', 'tailored', 'review'):
            questions += [i['ask'] for i in s['items']]
        elif s['key'] == 'symptoms':
            questions += [g['ask'] for g in s['groups']]
            for i in s['items']:
                if i['group'] or i['asked_in_urgent_screen']:
                    continue
                yes = 'when' if not i['severity'] else 'when, severity 0 to 10, and better, same or worse'
                questions.append(f"{i['ask']} If yes: {yes}." + (' If not: when did it stop?' if i['last_state'] == 'present' else ''))
    return [f'{n}. {q}' for n, q in enumerate(questions, 1)]


def pooled_patterns(rows, minimum):
    """Aggregate opted-in patients' current associations into cross-patient patterns.

    `rows` are (patient_id, method, exposure_pool_key, exposure_label,
    outcome_pool_key, outcome_label, direction, strength, effect, exposure_brand_keys).
    A medication association counts once under its generic and once under each
    brand key, so brand-level patterns are separate connections. A pattern is
    kept only when at least `minimum` distinct patients show it. The output
    carries no patient, record, date or free-text field."""
    expanded = []
    for patient, method, ekey, elabel, okey, olabel, direction, strength, effect, brands in rows:
        expanded.append((patient, method, ekey, elabel, okey, olabel, direction, strength, effect))
        expanded += [(patient, method, b, b.split(':', 1)[1].title() + ' (brand)', okey, olabel, direction, strength, effect)
                     for b in brands or ()]
    groups = {}
    for patient, method, ekey, elabel, okey, olabel, direction, strength, effect in expanded:
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
          coalesce(x.unit_ucum, x.unit_source) AS unit, x.concept_id, x.normalized->'source_details' AS details, x.recorded_at,
          c.label AS concept_label, c.basis, c.family, c.generic_name, c.brand_names
        FROM health.resources x LEFT JOIN health.concepts c ON c.patient_id = x.patient_id AND c.concept_id = x.concept_id
        WHERE x.patient_id = %s AND x.resource_id = ANY(%s) ORDER BY x.resource_id""", (patient, list(resource_ids))).fetchall()

    def label(r):
        # A key concept is named by its key, not by one report's sentence.
        if r['basis'] == 'source_key':
            return r['concept_id'].rsplit(':', 1)[1].replace('_', ' ')
        return r['concept_label'] or r['name']

    concepts = {r['concept_id']: (r['basis'], r['family'], label(r), r['generic_name'], tuple(r['brand_names'] or ()))
                for r in rows if r['concept_id']}
    observations, reports = [], []
    for r in rows:
        if r['category'] not in ('symptom_observation', 'reported_exposure') or not r['concept_id']:
            continue
        source = r['details'] or {}
        details = source.get('details') or {}
        reported = as_date(source.get('reported_local_date') or r['recorded_at'])
        kind = 'symptom' if r['category'] == 'symptom_observation' else 'exposure'
        if reported:
            reports.append({'concept': r['concept_id'], 'label': label(r), 'kind': kind, 'reported': reported, 'assertion': r['assertion'],
                            'scope': details.get('temporal_scope'), 'dated': bool(r['clinical_date']),
                            'key': details.get('symptom_key') or details.get('exposure_key')})
        # A statement about the present ("constipation persists") describes the day it was made.
        day = r['clinical_date'] or (reported if details.get('temporal_scope') == 'current_state' else None)
        if day and r['assertion'] in ('present', 'absent'):
            amount = details.get('amount') or {}
            observations.append({'rid': r['resource_id'], 'concept': r['concept_id'], 'label': label(r), 'family': r['family'],
                                 'kind': kind, 'date': day, 'assertion': r['assertion'],
                                 'date_basis': 'occurrence' if r['clinical_date'] else 'current_state_on_report_date',
                                 'amount': amount.get('value') if isinstance(amount.get('value'), (int, float)) else None})
    # Workouts and short nights are exposures the engine derives from activity and sleep reports.
    for r in rows:
        details = ((r['details'] or {}).get('details') or {})
        if r['category'] == 'exercise_session' and r['clinical_date']:
            done = details.get('status') == 'completed_reported'
            minutes = (details.get('duration') or {}).get('value')
            observations.append({'rid': r['resource_id'], 'concept': WORKOUT, 'label': 'Workout', 'family': 'exposure', 'kind': 'exposure',
                                 'date': r['clinical_date'], 'assertion': 'present' if done else 'absent', 'date_basis': 'occurrence',
                                 'amount': minutes if done else 0, 'amount_unit': 'min', 'sum_amounts': True})
        if r['category'] == 'sleep_observation' and r['clinical_date'] and r['numeric_value'] is not None:
            observations.append({'rid': r['resource_id'], 'concept': SHORT_SLEEP, 'label': f'Short sleep (under {SHORT_SLEEP_HOURS:g} h)',
                                 'family': 'exposure', 'kind': 'exposure', 'date': r['clinical_date'], 'date_basis': 'occurrence',
                                 'assertion': 'present' if r['numeric_value'] < SHORT_SLEEP_HOURS else 'absent',
                                 'amount': r['numeric_value'], 'amount_unit': 'h slept'})
    for derived, name in ((WORKOUT, 'Workout'), (SHORT_SLEEP, f'Short sleep (under {SHORT_SLEEP_HOURS:g} h)')):
        if any(o['concept'] == derived for o in observations):
            concepts[derived] = ('engine_derived', 'exposure', name, None, ())
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
    reported_measurements = {}
    for r in rows:
        if r['category'] in ('anthropometric_measurement', 'vital_sign_measurement') and r['concept_id'] and r['clinical_date']:
            m = reported_measurements.setdefault(r['concept_id'], {'label': label(r), 'unit': r['unit'], 'dates': set()})
            m['dates'].add(r['clinical_date'])
            m['last'] = max(m['dates'])
    tracking = []
    for r in rows:
        if r['category'] == 'check_in_tracking':
            source = r['details'] or {}
            d = source.get('details') or {}
            tracking.append({'rid': r['resource_id'], 'kind': d['item_kind'], 'key': d['item_key'], 'action': d['action'], 'label': d['label'],
                             'question': d.get('question'), 'value': d.get('setting_value'), 'reported_at': source.get('reported_at') or '',
                             'reported': as_date(source.get('reported_local_date') or r['recorded_at'])})
    reported_dates = [as_date(d) for d in ((r['details'] or {}).get('reported_local_date') for r in rows) if d]
    return {'observations': observations, 'reports': reports, 'medications': list(medications.values()), 'series': series,
            'concepts': concepts, 'reported_measurements': reported_measurements, 'last_reported': max(reported_dates, default=None),
            'tracking': tracking}
