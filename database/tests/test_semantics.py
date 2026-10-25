import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from semantics import Registry, assign_concepts, normalize

ONTOLOGY = Path(__file__).resolve().parents[2] / 'template' / 'ontology'
LOINC = 'http://loinc.org'


def template_registry():
    load = lambda name, key: json.loads((ONTOLOGY / name).read_text())[key]
    return Registry(load('categories.json', 'categories'), load('relations.json', 'relations'),
                    load('clinical-types.json', 'clinical_types'), load('concepts.json', 'concepts'))


class TemplateRegistryTest(unittest.TestCase):
    def test_template_registry_is_consistent(self):
        registry = template_registry()
        self.assertIn('blood_donor_screening', registry.categories)
        self.assertEqual(registry.aliases['vital-signs'], 'blood_donor_screening')
        self.assertEqual(registry.common_type(['vital_sign', 'laboratory_result']), 'observation')
        self.assertEqual(registry.family('medication_statement'), 'medication')

    def test_invalid_registry_is_refused(self):
        load = lambda name, key: json.loads((ONTOLOGY / name).read_text())[key]
        categories = load('categories.json', 'categories') + [{'category': 'x', 'clinical_type': 'missing'}]
        with self.assertRaises(ValueError):
            Registry(categories, load('relations.json', 'relations'), load('clinical-types.json', 'clinical_types'), [])


class ConceptTest(unittest.TestCase):
    def setUp(self):
        self.registry = template_registry()

    def test_weight_joins_across_sources_and_keeps_clinical_types(self):
        concepts, assigned = assign_concepts(self.registry, {
            'clinic': ('vitals', 'Body weight', LOINC, '29463-7', 'source_record'),
            'patient': ('anthropometric_measurement', 'Body weight', None, None, 'reported'),
            'pulse': ('blood_donor_screening', 'pulse', None, None, 'source_record'),
            'iron': ('blood_donor_screening', 'iron count', None, None, 'source_record'),
        })
        self.assertEqual(assigned['clinic'][:2], ('concept:body_weight', 'source_code'))
        self.assertEqual(assigned['patient'][:2], ('concept:body_weight', 'curated_name_match'))
        self.assertEqual(assigned['clinic'][2], 'body_measurement')
        self.assertEqual(assigned['pulse'][0], 'concept:heart_rate')
        self.assertEqual(assigned['iron'][0], 'concept:donor_hemoglobin')
        self.assertEqual(assigned['iron'][2], 'laboratory_result')
        # Curated standard codes stay on the concept and are labelled as curated.
        self.assertEqual(concepts['concept:donor_hemoglobin']['codings'][0]['basis'], 'curated_standard_code')

    def test_uncoded_lab_joins_a_uniquely_coded_name_but_not_an_ambiguous_one(self):
        concepts, assigned = assign_concepts(self.registry, {
            'quest': ('laboratory', 'CREATININE', LOINC, '2160-0', 'source_record'),
            'labcorp': ('laboratory', 'Creatinine', None, None, 'source_record'),
            'a': ('laboratory', 'Glucose', LOINC, '2345-7', 'source_record'),
            'b': ('lab_results', 'Glucose', LOINC, '2339-0', 'source_record'),
            'c': ('laboratory', 'Glucose', None, None, 'source_record'),
        })
        self.assertEqual(assigned['labcorp'][:2], (f'concept:code:{LOINC}|2160-0', 'name_matches_coded_source_record'))
        self.assertEqual(assigned['c'][:2], ('concept:name:measurement:glucose', 'source_name'))
        self.assertEqual(concepts[f'concept:code:{LOINC}|2160-0']['label'], 'CREATININE')

    def test_records_without_a_concept_family_or_absence_statements_get_none(self):
        _, assigned = assign_concepts(self.registry, {
            'note': ('patient_hypothesis', 'Swelling from alcohol', None, None, 'hypothesis'),
            'none': ('medications', 'No known medications', None, None, 'source_absence_statement'),
            'unnamed': ('lab_results', 'lab_results', None, None, 'source_record'),
        })
        self.assertIsNone(assigned['unnamed'][0])
        self.assertEqual(assigned['note'], (None, None, 'patient_hypothesis'))
        self.assertIsNone(assigned['none'][0])
        self.assertEqual(normalize('  Body  Weight. '), 'body weight')


    def test_curated_concepts_join_only_records_of_their_family(self):
        load = lambda name, key: json.loads((ONTOLOGY / name).read_text())[key]
        medication = {'concept': 'medication:furosemide', 'label': 'Furosemide', 'clinical_type': 'medication_use', 'family': 'medication',
                      'codings': [], 'match': {'name_patterns': [r'^furosemide\b']}}
        registry = Registry(load('categories.json', 'categories'), load('relations.json', 'relations'),
                            load('clinical-types.json', 'clinical_types'), [medication])
        _, assigned = assign_concepts(registry, {
            'regimen': ('medication_regimen', 'Furosemide', None, None, 'reported'),
            'response': ('medication_response', 'Furosemide reduced swelling', None, None, 'reported'),
        })
        self.assertEqual(assigned['regimen'][:2], ('concept:medication:furosemide', 'curated_name_match'))
        self.assertEqual(assigned['regimen'][2], 'medication_statement')
        self.assertEqual(assigned['response'], (None, None, 'medication_response'))


class EdgeRuleTest(unittest.TestCase):
    def setUp(self):
        self.registry = template_registry()
        kinds = {'r1': ('@Resource', 'medication_change', 'medication_change'), 'reg': ('@Resource', 'medication_regimen', 'medication_statement'),
                 'sym': ('@Resource', 'symptom_observation', 'symptom'), 'pat': ('@Patient', None, None)}
        self.endpoint = kinds.get

    def test_valid_edges_pass(self):
        self.assertEqual(self.registry.edge_problems([('r1', 'reg', 'ABOUT_MEDICATION', {'aspect': 'change'})], self.endpoint), [])

    def test_alias_missing_qualifier_and_wrong_endpoints_fail(self):
        problems = self.registry.edge_problems([
            ('r1', 'reg', 'CHANGE_OF_MEDICATION', {}),
            ('r1', 'reg', 'ABOUT_MEDICATION', {}),
            ('sym', 'pat', 'ABOUT_MEDICATION', {'aspect': 'change'}),
            ('r1', 'reg', 'BELONGS_TO_PATIENT', {}),
        ], self.endpoint)
        text = ' '.join(problems)
        self.assertIn('alias relation, use ABOUT_MEDICATION', text)
        self.assertIn('invalid qualifier aspect=None', text)
        self.assertIn('from symptom_observation is outside its domain', text)
        self.assertIn('retired relation', text)


if __name__ == '__main__':
    unittest.main()
