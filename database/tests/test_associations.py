from datetime import date, timedelta
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from associations import check_in_plan, find_associations, fisher_greater, pool_key, pooled_patterns, slope_per_week

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
                 'Swelling (edema)', 'positive', strength, {'risk_ratio': 2.0 + i}) for i in range(patients)]

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

    def test_pool_keys(self):
        self.assertEqual(pool_key('concept:symptom:swelling', 'curated_registry', 'symptom', 'Swelling'), 'concept:symptom:swelling')
        self.assertEqual(pool_key('concept:key:symptom:night_sweats', 'source_key', 'symptom', 'night sweats'), 'concept:key:symptom:night_sweats')
        self.assertIsNone(pool_key('concept:name:symptom:my odd note', 'source_name', 'symptom', 'My odd note'))
        self.assertEqual(pool_key('concept:medication:testosterone', 'curated_registry', 'medication', 'Testosterone (all forms)'), 'medication:testosterone')
        self.assertEqual(pool_key('concept:name:medication:tb-500', 'source_name', 'medication', 'TB-500'), 'medication:tb-500')
        self.assertEqual(pool_key('x', 'source_code', 'medication', 'anastrozole (ARIMIDEX) 1 mg tablet'), 'medication:anastrozole')


class CheckInTest(unittest.TestCase):
    def test_plan_lists_unreported_days_for_associated_concepts(self):
        facts = swelling_facts()
        plan = check_in_plan(facts, find_associations(facts), D(24), window=5)
        items = {p['concept']: p for p in plan}
        self.assertEqual(items['concept:symptom:swelling']['unreported_days'], ['2026-10-24', '2026-10-23'])
        self.assertEqual(items['concept:exposure:alcohol']['reported_days'], 3)
        self.assertIn('Record "none" too', items['concept:symptom:swelling']['ask'])

    def test_fisher(self):
        self.assertAlmostEqual(fisher_greater(5, 0, 0, 3), 1 / 56)
        self.assertEqual(fisher_greater(0, 3, 3, 0), 1.0)


if __name__ == '__main__':
    unittest.main()
