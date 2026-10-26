from datetime import date, timedelta
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from associations import SHORT_SLEEP, WORKOUT, check_in_plan, daily, find_associations, fisher_greater, pool_key, pooled_patterns, slope_per_week

D = lambda day: date(2026, 10, day)


def observation(rid, concept, kind, day, assertion, amount=None, label=None):
    return {'rid': rid, 'concept': concept, 'label': label or concept.split(':')[-1], 'family': kind, 'kind': kind,
            'date': D(day), 'assertion': assertion, 'amount': amount}


def medication(concept, label, status, since, events=()):
    return {'concept': concept, 'label': label, 'status': status, 'known_since': since, 'events': list(events)}


def swelling_facts():
    """Swelling on drinking nights, none on dry days; testosterone throughout; HGH stopped just before."""
    drinks = {15: 4, 16: 6, 17: 10, 18: 10, 19: 0, 20: 0, 21: 8, 22: 0}
    swelling = {15: 'present', 16: 'present', 17: 'present', 18: 'present', 19: 'absent', 20: 'absent', 21: 'present', 22: 'absent'}
    observations = [observation(f'a{d}', 'concept:exposure:alcohol', 'exposure', d, 'present' if n else 'absent', n, 'Alcohol intake')
                    for d, n in drinks.items()]
    observations += [observation(f's{d}', 'concept:symptom:swelling', 'symptom', d, state, label='Swelling') for d, state in swelling.items()]
    medications = [
        medication('concept:medication:testosterone', 'Testosterone', 'active', D(1)),
        medication('concept:medication:hgh', 'HGH', 'stopped', D(1),
                   [{'date': D(10), 'status': 'stopped', 'previous': 'active', 'change_type': 'stopped', 'rid': 'hgh-stop'}]),
    ]
    return {'observations': observations, 'medications': medications, 'series': {}, 'concepts': {}}


class EpisodeTest(unittest.TestCase):
    def setUp(self):
        self.found = {(a['method'], a['exposure_concept']): a for a in find_associations(swelling_facts())}

    def test_varying_exposure_outranks_constant_background(self):
        alcohol = self.found[('exposure_episode_contrast', 'concept:exposure:alcohol')]
        self.assertEqual(alcohol['strength'], 'strong')
        self.assertEqual(alcohol['effect']['exposed_with'], 5)
        self.assertEqual(alcohol['effect']['unexposed_with'], 0)
        self.assertGreater(alcohol['effect']['mean_amount_on_symptom_days'], alcohol['effect']['mean_amount_on_symptom_free_days'])
        self.assertIn('varies with the symptom', alcohol['statement'])
        self.assertIn('not proof of cause', alcohol['statement'])
        # Every record of a symptom day with drinking supports it; none contradicts it.
        self.assertIn('s17', alcohol['supporting_records'])
        self.assertIn('a17', alcohol['supporting_records'])
        self.assertEqual(alcohol['contrary_records'], [])
        ranked = {c['exposure_concept']: c for c in alcohol['competing']}
        self.assertFalse(ranked['concept:medication:testosterone']['testable'])

    def test_constant_medication_cannot_explain_a_symptom_that_comes_and_goes(self):
        testosterone = self.found[('constant_background', 'concept:medication:testosterone')]
        self.assertEqual(testosterone['effect']['background_state'], 'present')
        self.assertEqual(testosterone['effect']['exposed_without'], 3)
        self.assertEqual(testosterone['direction'], 'none')
        self.assertIn('cannot explain', testosterone['statement'])

    def test_recently_stopped_medication_is_reported_as_absent_background(self):
        hgh = self.found[('constant_background', 'concept:medication:hgh')]
        self.assertEqual(hgh['effect']['background_state'], 'absent')
        self.assertEqual(hgh['effect']['days_from_stop_to_first_symptom'], 5)
        self.assertIn('hgh-stop', hgh['supporting_records'])
        self.assertIn('lingering effects', hgh['statement'])

    def test_unreported_days_are_unknown_not_symptom_free(self):
        facts = swelling_facts()
        facts['observations'] = [o for o in facts['observations'] if not (o['kind'] == 'symptom' and o['assertion'] == 'absent')]
        self.assertEqual(find_associations(facts), [])

    def test_results_are_deterministic(self):
        self.assertEqual(find_associations(swelling_facts()), find_associations(swelling_facts()))


class DerivedExposureTest(unittest.TestCase):
    def test_workouts_sum_per_day_and_short_sleep_is_compared(self):
        sessions = [{'rid': 'w1', 'concept': WORKOUT, 'label': 'Workout', 'family': 'exposure', 'kind': 'exposure', 'date': D(1),
                     'assertion': 'present', 'amount': 40, 'amount_unit': 'min', 'sum_amounts': True},
                    {'rid': 'w2', 'concept': WORKOUT, 'label': 'Workout', 'family': 'exposure', 'kind': 'exposure', 'date': D(1),
                     'assertion': 'present', 'amount': 40, 'amount_unit': 'min', 'sum_amounts': True}]
        self.assertEqual(daily(sessions)[WORKOUT][D(1)]['amount'], 80)
        hours = {1: 5, 2: 5.5, 3: 8, 4: 7.5, 5: 5, 6: 8, 7: 6, 8: 8}
        headache = {1: 'present', 2: 'present', 3: 'absent', 4: 'absent', 5: 'present', 6: 'absent', 7: 'present', 8: 'absent'}
        facts = {'medications': [], 'series': {}, 'concepts': {},
                 'observations': [{'rid': f'n{d}', 'concept': SHORT_SLEEP, 'label': 'Short sleep (under 7 h)', 'family': 'exposure', 'kind': 'exposure',
                                   'date': D(d), 'assertion': 'present' if h < 7 else 'absent', 'amount': h, 'amount_unit': 'h slept'} for d, h in hours.items()]
                                 + [observation(f'h{d}', 'concept:key:symptom:headache', 'symptom', d, st, label='headache') for d, st in headache.items()]}
        [a] = find_associations(facts)
        self.assertEqual((a['exposure_concept'], a['strength']), (SHORT_SLEEP, 'strong'))
        self.assertEqual(a['effect']['amount_unit'], 'h slept')
        self.assertLess(a['effect']['mean_amount_on_symptom_days'], a['effect']['mean_amount_on_symptom_free_days'])
        self.assertEqual(pool_key(SHORT_SLEEP, 'engine_derived', 'exposure', 'Short sleep'), SHORT_SLEEP)


class SeriesTest(unittest.TestCase):
    def weights(self, values):
        return {'concept:body_weight': {'label': 'Body weight', 'unit': '[lb_av]',
                                        'points': [(D(1) + timedelta(days=i), v, f'w{i}') for i, v in values]}}

    def test_on_off_on_reversal_is_strong(self):
        events = [{'date': D(1), 'status': 'active', 'previous': 'stopped', 'change_type': 'started', 'rid': 'start'},
                  {'date': D(11), 'status': 'stopped', 'previous': 'active', 'change_type': 'stopped', 'rid': 'stop'},
                  {'date': D(21), 'status': 'active', 'previous': 'stopped', 'change_type': 'restarted', 'rid': 'restart'}]
        values = [(i, 250 - i) for i in range(0, 10)] + [(i, 241 + 0.5 * (i - 10)) for i in range(10, 20)] + [(i, 246 - (i - 20)) for i in range(20, 30)]
        facts = {'observations': [], 'series': self.weights(values), 'concepts': {},
                 'medications': [medication('concept:medication:retatrutide', 'Retatrutide', 'active', D(1), events)]}
        [a] = find_associations(facts)
        self.assertEqual((a['method'], a['strength'], a['direction']), ('exposure_series_change', 'strong', 'decrease'))
        self.assertLess(a['effect']['mean_slope_on'], 0)
        self.assertGreater(a['effect']['mean_slope_off'], 0)
        self.assertIn('reversed when it stopped and resumed when it restarted', a['statement'])

    def test_a_few_days_after_a_single_change_is_insufficient(self):
        events = [{'date': D(10), 'status': 'active', 'previous': 'stopped', 'change_type': 'restarted', 'rid': 'restart'}]
        values = [(i, 245) for i in range(0, 9)] + [(10, 241), (11, 243), (12, 248)]
        facts = {'observations': [], 'series': self.weights(values), 'concepts': {},
                 'medications': [medication('concept:medication:retatrutide', 'Retatrutide', 'active', D(1), events)]}
        [a] = find_associations(facts)
        self.assertEqual(a['strength'], 'insufficient')
        self.assertTrue(any('only 3 day(s)' in limit for limit in a['limitations']))
        self.assertIsNone(slope_per_week([(D(1), 1), (D(2), 5), (D(3), 9)]))


class PoolingTest(unittest.TestCase):
    def rows(self, patients, strength='moderate'):
        return [(f'person-{i}', 'exposure_episode_contrast', 'concept:exposure:alcohol', 'Alcohol intake', 'concept:symptom:swelling',
                 'Swelling (edema)', 'positive', strength, {'risk_ratio': 2.0 + i}, []) for i in range(patients)]

    def test_patterns_need_five_people_and_carry_no_identity(self):
        self.assertEqual(pooled_patterns(self.rows(4), 5), [])
        [pattern] = pooled_patterns(self.rows(5), 5)
        self.assertEqual(pattern['contributors'], 5)
        self.assertEqual(pattern['median_effect'], {'measure': 'risk_ratio', 'value': 4.0})
        self.assertNotIn('person-', repr(pattern))
        self.assertEqual(pooled_patterns(self.rows(5), 6), [])

    def test_insufficient_and_unshareable_associations_are_not_pooled(self):
        self.assertEqual(pooled_patterns(self.rows(6, 'insufficient'), 5), [])
        rows = [r[:2] + (None,) + r[3:] for r in self.rows(6)]
        self.assertEqual(pooled_patterns(rows, 5), [])

    def test_one_person_counts_once(self):
        rows = self.rows(4) + [('person-0',) + self.rows(1)[0][1:]]
        self.assertEqual(pooled_patterns(rows, 5), [])

    def test_medications_pool_by_generic_and_separately_by_brand(self):
        # Five people on linaclotide: three record the Linzess brand, two only the generic.
        rows = [(f'person-{i}', 'exposure_episode_contrast', 'medication:linaclotide', 'Linaclotide', 'concept:key:symptom:watery_diarrhea',
                 'watery diarrhea', 'positive', 'moderate', {'risk_ratio': 3.0}, ['medication-brand:linzess'] if i < 3 else []) for i in range(5)]
        [pattern] = pooled_patterns(rows, 5)
        self.assertEqual((pattern['exposure_key'], pattern['contributors']), ('medication:linaclotide', 5))
        brand = {p['exposure_key']: p for p in pooled_patterns(rows, 3)}['medication-brand:linzess']
        self.assertEqual((brand['exposure_key'], brand['exposure_label'], brand['contributors']), ('medication-brand:linzess', 'Linzess (brand)', 3))

    def test_pool_keys(self):
        self.assertEqual(pool_key('concept:symptom:swelling', 'curated_registry', 'symptom', 'Swelling'), 'concept:symptom:swelling')
        self.assertEqual(pool_key('concept:key:symptom:night_sweats', 'source_key', 'symptom', 'night sweats'), 'concept:key:symptom:night_sweats')
        self.assertIsNone(pool_key('concept:name:symptom:my odd note', 'source_name', 'symptom', 'My odd note'))
        self.assertEqual(pool_key('concept:medication:testosterone', 'curated_registry', 'medication', 'Testosterone (all forms)'), 'medication:testosterone')
        self.assertEqual(pool_key('concept:medication:linaclotide', 'curated_registry', 'medication', 'Linaclotide (Linzess)', 'linaclotide'), 'medication:linaclotide')
        self.assertEqual(pool_key('concept:name:medication:linzess', 'source_name', 'medication', 'Linzess', 'linaclotide'), 'medication:linaclotide')
        self.assertEqual(pool_key('concept:name:medication:tb-500', 'source_name', 'medication', 'TB-500'), 'medication:tb-500')
        self.assertEqual(pool_key('x', 'source_code', 'medication', 'anastrozole (ARIMIDEX) 1 mg tablet'), 'medication:anastrozole')


class CheckInTest(unittest.TestCase):
    def plan(self, facts=None, today=D(24)):
        facts = facts or swelling_facts()
        return {s['key']: s for s in check_in_plan(facts, find_associations(facts), today, window=5)['steps']}

    def test_agenda_follows_the_agreed_order(self):
        facts = swelling_facts()
        facts['reported_measurements'] = {'concept:body_weight': {'label': 'Body weight', 'unit': '[lb_av]', 'dates': {D(22)}, 'last': D(22)}}
        facts['last_reported'] = D(22)
        plan = check_in_plan(facts, find_associations(facts), D(24), window=5)
        self.assertEqual([s['key'] for s in plan['steps']],
                         ['opening', 'urgent', 'open', 'measurements', 'sleep', 'workout', 'medications', 'exposures', 'symptoms', 'missed_days', 'close'])
        steps = {s['key']: s for s in plan['steps']}
        self.assertIn('Thursday, October 22', steps['opening']['say'])
        self.assertIn('since Thursday, October 22', steps['medications']['asks'][0])
        self.assertIn('as-needed', steps['medications']['asks'][1])
        self.assertEqual(steps['exposures']['items'][0]['concept'], 'concept:exposure:alcohol')
        self.assertIn('Do you want to fill', steps['missed_days']['ask'])
        self.assertEqual(steps['missed_days']['days'], ['2026-10-23'])

    def test_a_symptom_present_last_time_asks_when_it_stopped(self):
        facts = swelling_facts()
        facts['observations'].append(observation('s23', 'concept:symptom:swelling', 'symptom', 23, 'present', label='Swelling'))
        swelling = self.plan(facts)['symptoms']['items'][0]
        self.assertEqual((swelling['last_state'], swelling['last_state_date']), ('present', '2026-10-23'))
        self.assertIn('when it stopped', swelling['if_no'])
        dry = self.plan()['symptoms']['items'][0]
        self.assertEqual(dry['if_no'], 'Record it as absent today.')

    def test_active_symptoms_without_dated_reports_are_asked_about(self):
        facts = swelling_facts()
        facts['reports'] = [
            {'concept': 'concept:symptom:constipation', 'label': 'Constipation', 'kind': 'symptom', 'reported': D(1), 'assertion': 'present'},
            {'concept': 'concept:key:symptom:very_dark_stool', 'label': 'very dark stool', 'kind': 'symptom', 'reported': D(11), 'assertion': 'present'},
            # Reported gone since, so no longer active.
            {'concept': 'concept:key:symptom:night_sweats', 'label': 'night sweats', 'kind': 'symptom', 'reported': D(5), 'assertion': 'present'},
            {'concept': 'concept:key:symptom:night_sweats', 'label': 'night sweats', 'kind': 'symptom', 'reported': D(9), 'assertion': 'absent'},
            # Too long ago, a single day, or lifelong: not active.
            {'concept': 'concept:key:symptom:dry_eyes', 'label': 'dry eyes', 'kind': 'symptom', 'reported': date(2026, 6, 1), 'assertion': 'present'},
            {'concept': 'concept:key:symptom:post_cardio_sweating', 'label': 'post cardio sweating', 'kind': 'symptom', 'reported': D(20),
             'assertion': 'present', 'scope': 'current_state', 'dated': True},
            {'concept': 'concept:key:symptom:sock_marks', 'label': 'sock marks', 'kind': 'symptom', 'reported': D(20),
             'assertion': 'present', 'scope': 'lifelong_history'},
        ]
        plan = self.plan(facts)
        items = {i['label']: i for i in plan['symptoms']['items']}
        self.assertEqual(list(items)[0], 'Swelling')
        self.assertEqual(set(items), {'Swelling', 'Constipation', 'very dark stool'})
        # A reported red-flag symptom is folded into the urgent screen item it overlaps, and not asked again.
        self.assertTrue(items['very dark stool']['asked_in_urgent_screen'])
        self.assertEqual(len(plan['urgent']['items']), 8)
        self.assertIn('you reported very dark stool on 2026-10-11', next(i for i in plan['urgent']['items'] if i.startswith('black or tarry stool')))
        self.assertTrue(items['Swelling']['severity'])

    def test_overlapping_bowel_symptoms_are_one_question_with_one_bristol_type(self):
        facts = swelling_facts()
        facts['reports'] = [{'concept': c, 'label': l, 'kind': 'symptom', 'reported': D(1), 'assertion': 'present'} for c, l in (
            ('concept:symptom:constipation', 'Constipation'), ('concept:key:symptom:small_ribbon_shaped_stool', 'small ribbon shaped stool'),
            ('concept:key:symptom:watery_diarrhea', 'watery diarrhea'), ('concept:symptom:bloating_trapped_gas', 'Bloating or trapped gas'),
            ('concept:key:symptom:more_yellow_urine', 'more yellow urine'))]
        plan = check_in_plan(facts, find_associations(facts), D(24), window=5)
        steps = {s['key']: s for s in plan['steps']}
        (bowel,) = steps['symptoms']['groups']
        self.assertEqual(bowel['key'], 'bowel')
        self.assertTrue(bowel['bristol'])
        self.assertEqual(len(bowel['items']), 4)
        self.assertEqual(bowel['ask'].count('Bristol'), 1)
        items = {i['label']: i for i in steps['symptoms']['items']}
        # Seen, not felt: no 0 to 10 scale. A lone urinary symptom is asked by itself.
        self.assertFalse(items['small ribbon shaped stool']['severity'])
        self.assertTrue(items['Constipation']['severity'])
        self.assertIsNone(items['more yellow urine']['group'])
        self.assertIn('Bloating or trapped gas'.lower(), bowel['follow_up'])
        self.assertNotIn('ribbon', bowel['follow_up'].split('severity')[0])
        questions = plan['questionnaire']
        self.assertTrue(questions[0].startswith('1. Before we start'))
        self.assertEqual([q.split('.')[0] for q in questions], [str(n) for n in range(1, len(questions) + 1)])
        self.assertEqual(sum('Bristol' in q for q in questions), 1)
        self.assertFalse(any('ribbon' in q and 'Do you still' in q for q in questions))

    def test_a_symptom_reported_at_the_last_check_in_is_asked_again(self):
        facts = swelling_facts()
        facts['last_reported'] = D(23)
        facts['reports'] = [{'concept': 'concept:key:symptom:headache', 'label': 'headache', 'kind': 'symptom', 'reported': D(23),
                             'assertion': 'present', 'scope': 'dated_episode', 'dated': True}]
        facts['observations'].append(observation('h23', 'concept:key:symptom:headache', 'symptom', 23, 'present', label='headache'))
        items = {i['label']: i for i in self.plan(facts)['symptoms']['items']}
        self.assertIn('headache', items)
        self.assertIn('when it stopped', items['headache']['if_no'])

    def test_measurements_and_drinks_are_asked_only_when_tracked(self):
        facts = swelling_facts()
        facts['observations'] = [o for o in facts['observations'] if o['kind'] == 'symptom']
        plan = self.plan(facts)
        self.assertNotIn('measurements', plan)
        self.assertNotIn('exposures', plan)
        self.assertIn('sleep', plan)
        self.assertIn('workout', plan)

    def test_fisher(self):
        self.assertAlmostEqual(fisher_greater(5, 0, 0, 3), 1 / 56)
        self.assertEqual(fisher_greater(0, 3, 3, 0), 1.0)


if __name__ == '__main__':
    unittest.main()
