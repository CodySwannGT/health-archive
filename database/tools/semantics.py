"""Clinical types, concepts and relation rules from the ontology registry.

Pure functions over the registry files, so projection and verification apply
the same rules. Nothing here reads or writes the database.
"""
import re

KINDS = {'@Patient', '@User', '@Document', '@OntologyCategory', '@Concept', '@ClinicalType'}


class Registry:
    def __init__(self, categories, relations, clinical_types, concepts):
        self.categories = {c['category']: c for c in categories}
        self.relations = {r['relation']: r for r in relations}
        self.types = {t['clinical_type']: t for t in clinical_types}
        self.concepts = concepts
        self.aliases = {alias: c['category'] for c in categories for alias in c.get('aliases', [])}
        self._check()
        self._patterns = [[re.compile(p) for p in c['match'].get('name_patterns', [])] for c in concepts]
        self._codes = {}
        for index, c in enumerate(concepts):
            for code in c['match'].get('codes', []):
                if self._codes.setdefault(code, index) != index:
                    raise ValueError(f'Code {code} is listed under more than one concept')

    def _check(self):
        problems = []
        for t in self.types.values():
            if t['parent'] and t['parent'] not in self.types:
                problems.append(f"type {t['clinical_type']}: unknown parent {t['parent']}")
        for c in self.categories.values():
            if c.get('clinical_type') not in self.types:
                problems.append(f"category {c['category']}: unknown clinical type {c.get('clinical_type')}")
        for r in self.relations.values():
            if r.get('status', 'active') not in ('active', 'alias', 'retired'):
                problems.append(f"relation {r['relation']}: unknown status")
            if r.get('status') == 'alias' and self.relations.get(r.get('alias_of'), {}).get('status', 'active') != 'active':
                problems.append(f"relation {r['relation']}: alias of an inactive relation")
            for entry in r.get('domain', []) + r.get('range', []):
                if not (entry in KINDS or entry == '@Resource' or entry in self.categories
                        or (entry.startswith('type:') and entry[5:] in self.types)):
                    problems.append(f"relation {r['relation']}: unknown endpoint class {entry}")
        for c in self.concepts:
            if c['clinical_type'] not in self.types:
                problems.append(f"concept {c['concept']}: unknown clinical type {c['clinical_type']}")
        if problems:
            raise ValueError('Invalid ontology registry: ' + '; '.join(problems))

    def ancestors(self, clinical_type):
        chain = []
        while clinical_type:
            chain.append(clinical_type)
            clinical_type = self.types[clinical_type]['parent']
        return chain

    def family(self, clinical_type):
        for t in self.ancestors(clinical_type):
            if self.types[t]['concept_family']:
                return self.types[t]['concept_family']
        return None

    def common_type(self, clinical_types):
        """The narrowest type every given type falls under."""
        shared = None
        for t in clinical_types:
            chain = self.ancestors(t)
            shared = chain if shared is None else [x for x in shared if x in chain]
        return shared[0] if shared else None

    def concept_family(self, concept):
        return concept.get('family') or self.family(concept['clinical_type'])

    def curated(self, category, name, code):
        """Index of the curated concept a record matches, and how it matched.

        Only records of the concept's own family match, so a patient note that
        names a medication does not become a record of that medication."""
        family = self.family(self.categories[category]['clinical_type'])
        if not family:
            return None, None
        if code and code in self._codes:
            index = self._codes[code]
            limit = self.concepts[index]['match'].get('categories')
            if (not limit or category in limit) and self.concept_family(self.concepts[index]) == family:
                return index, 'source_code'
        for index, c in enumerate(self.concepts):
            match = c['match']
            if (match.get('categories') and category not in match['categories']) or self.concept_family(c) != family:
                continue
            if name in match.get('names', []) or any(p.search(name) for p in self._patterns[index]):
                return index, 'curated_name_match'
        return None, None

    def record_type(self, category, concept_type):
        """A record's clinical type: its concept's type, unless the category's default is narrower."""
        default = self.categories[category]['clinical_type']
        if concept_type and concept_type not in self.ancestors(default):
            return concept_type
        return default

    def allows(self, entries, endpoint):
        """Whether an endpoint, given as (kind, category, clinical_type), falls in a domain or range."""
        kind, category, clinical_type = endpoint
        for entry in entries:
            if entry == kind:
                return True
            if kind == '@Resource' and (entry == '@Resource' or entry == category
                                        or (entry.startswith('type:') and entry[5:] in self.ancestors(clinical_type))):
                return True
        return False

    def edge_problems(self, edges, endpoint):
        """Edges that use an inactive relation, lack a required qualifier, or fall outside the relation's domain and range."""
        problems = []
        for a, b, relation, qualifiers in edges:
            r = self.relations.get(relation)
            if not r:
                problems.append(f'{relation}: unregistered')
                continue
            if r.get('status', 'active') != 'active':
                problems.append(f"{relation}: {r['status']} relation" + (f", use {r['alias_of']}" if r.get('alias_of') else ''))
                continue
            for name, rule in r.get('qualifiers', {}).items():
                value = qualifiers.get(name)
                if (value is None and rule.get('required')) or (value is not None and 'enum' in rule and value not in rule['enum']):
                    problems.append(f'{relation}: invalid qualifier {name}={value}')
            for side, node, entries in (('from', a, r['domain']), ('to', b, r['range'])):
                point = endpoint(node)
                if point is None or not self.allows(entries, point):
                    problems.append(f'{relation}: {side} {point[1] or point[0] if point else node} is outside its {"domain" if side == "from" else "range"}')
        return sorted(set(problems))


def concept_eligible(registry, category, name, assertion):
    """Records of a type with a concept family join a concept, unless they state an absence
    or carry no name of their own (importers fall back to the category name)."""
    normalized = normalize(name)
    return bool(registry.family(registry.categories[category]['clinical_type']) and assertion != 'source_absence_statement'
                and normalized and normalized != normalize(category))


def normalize(name):
    return ' '.join(str(name or '').casefold().replace(' ', ' ').split()).strip(' .,:;')


def code_key(system, code):
    return f'{system}|{code}' if system and code else None


def assign_concepts(registry, resources):
    """Join records to concepts.

    `resources` maps a record's resource ID to (category, name, code_system, code, assertion).
    Returns the concepts and, per resource, (concept_id or None, basis, clinical_type).
    Matching order: the curated registry (by source code, then by name); the
    source's own code; a name the same source records carry with exactly one
    code; otherwise the normalized name within the concept family.
    """
    assigned, concepts = {}, {}
    pending = {}
    for rid, (category, name, system, code, assertion) in resources.items():
        default = registry.categories[category]['clinical_type']
        family = registry.family(default)
        normalized = normalize(name)
        index, basis = registry.curated(category, normalized, code_key(system, code))
        if index is not None:
            c = registry.concepts[index]
            cid = 'concept:' + c['concept']
            concepts.setdefault(cid, {'concept_id': cid, 'label': c['label'], 'clinical_type': c['clinical_type'], 'basis': 'curated_registry',
                                      'family': registry.concept_family(c), 'codings': list(c['codings']), 'note': c.get('note')})
            assigned[rid] = (cid, basis)
        elif concept_eligible(registry, category, name, assertion):
            pending[rid] = (family, normalized, code_key(system, code))
    # Names that source records carry with exactly one code, per family.
    coded_names = {}
    for rid, (family, normalized, key) in pending.items():
        if key:
            coded_names.setdefault((family, normalized), set()).add(key)
    for rid, (category, name, system, code, assertion) in resources.items():
        if key := code_key(system, code):
            cid = assigned.get(rid, (None,))[0]
            if cid:
                coded_names.setdefault((registry.family(registry.categories[category]['clinical_type']), normalize(name)), set()).add(cid)
    for rid, (family, normalized, key) in pending.items():
        if key:
            cid, basis = 'concept:code:' + key, 'source_code'
        elif len(coded_names.get((family, normalized), ())) == 1:
            target = next(iter(coded_names[(family, normalized)]))
            cid = target if target.startswith('concept:') else 'concept:code:' + target
            basis = 'name_matches_coded_source_record'
        else:
            cid, basis = f'concept:name:{family}:{normalized}', 'source_name'
        assigned[rid] = (cid, basis)
    # Labels, observed codings and clinical types of the derived concepts.
    members = {}
    for rid, (cid, basis) in assigned.items():
        members.setdefault(cid, []).append(rid)
    for cid, rids in members.items():
        names, codings = {}, {}
        for rid in rids:
            category, name, system, code, _ = resources[rid]
            names[name] = names.get(name, 0) + 1
            if system and code:
                codings[(system, code)] = {'system': system, 'code': code, 'basis': 'observed_in_source'}
        if cid in concepts:
            known = {(c['system'], c['code']) for c in concepts[cid]['codings']}
            concepts[cid]['codings'] += [c for k, c in sorted(codings.items()) if k not in known]
            continue
        types = {registry.categories[resources[rid][0]]['clinical_type'] for rid in rids}
        clinical_type = registry.common_type(types)
        label = sorted(names.items(), key=lambda item: (-item[1], item[0]))[0][0]
        concepts[cid] = {'concept_id': cid, 'label': label, 'clinical_type': clinical_type, 'family': registry.family(clinical_type),
                         'basis': 'source_code' if cid.startswith('concept:code:') else 'source_name',
                         'codings': [c for _, c in sorted(codings.items())], 'note': None}
    result = {}
    for rid in resources:
        cid, basis = assigned.get(rid, (None, None))
        curated_type = concepts[cid]['clinical_type'] if cid and concepts[cid]['basis'] == 'curated_registry' else None
        result[rid] = (cid, basis, registry.record_type(resources[rid][0], curated_type))
    return concepts, result
