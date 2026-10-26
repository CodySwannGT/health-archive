# Ontology

The ontology the health database enforces. It has four registry files.

| File | Registers |
| --- | --- |
| `categories.json` | Every record category. `domain` is where its records come from, `subject_area` groups categories by what they describe, `clinical_type` places it in the type hierarchy, and `schema_path` names the JSON Schema that validates its records. `aliases` keeps earlier names findable |
| `clinical-types.json` | The clinical type hierarchy, aligned with FHIR R4 resource types. A type with a `concept_family` groups its records under shared concepts |
| `concepts.json` | Curated concepts, such as body weight or blood pressure, and how records join them |
| `relations.json` | Every graph relation, its evidence semantics, and the endpoint classes it may connect (`domain` and `range`). Earlier names stay registered as `alias` or `retired` |

## How records are classified

Each record gets a **clinical type**: its category's type, unless its concept's type is more specific. Cleveland Clinic `vitals` holds weight and height, for example, so a weight there is a `body_measurement`, not a `vital_sign`.

Each record whose type has a concept family joins one **concept**, matched in this order:

1. A curated concept that lists the record's source code, its normalized name (`match.names`, `match.name_patterns`), or, for a symptom or exposure report, its structured key (`match.key_patterns`), optionally limited to `match.categories`. Only records of the concept's family match. `symptom:swelling` joins every generalized or limb swelling key, and `exposure:alcohol` every alcohol key.
2. A concept for the code the source supplied.
3. For a symptom or exposure report, a concept for its structured key, such as `concept:key:symptom:night_sweats`. A report that a symptom is absent joins the same concept as one that it is present.
4. A coded concept whose name the same kind of source records carry with exactly one code. Labcorp's uncoded "Creatinine" joins the LOINC 2160-0 concept that Quest and clinical records use.
5. A concept for the normalized name within the family.

A medication concept also carries a generic name and the brands its records name. A curated concept states them (`generic`, `brands`); otherwise a clinical list's `generic (BRAND)` form supplies them, and other names, such as "Linzess (linaclotide)", are left for a curated concept rather than guessed. Connections pool medications by generic, and each brand separately.

Each `REPRESENTS_CONCEPT` edge records which of these matched. Concepts are what the connections engine compares: `DERIVED_ASSOCIATION` edges link an exposure concept to an outcome concept, with the method and strength as qualifiers. They are computed, not reported, and never establish causality. A coding marked `curated_standard_code` is a mapping made in this registry. It is never copied into a record, whose `code` stays exactly what the source supplied. Joining a concept says two records measure the same thing. It does not say they are the same event.

## Rules enforced on every write

Reprojection fails when a record or relationship uses an unregistered entry, when an edge uses an `alias` or `retired` relation, when an edge lacks a required qualifier, or when an edge's endpoints fall outside the relation's domain and range. Endpoint classes are category names, `type:CLINICAL_TYPE` (including narrower types), and `@Resource`, `@Document`, `@Patient`, `@User`, `@OntologyCategory`, `@Concept` and `@ClinicalType`. A new distinction must be added here first.

These files are database-owned archive files. Change them by editing a local copy and running `healthdb put ontology/FILE`. Reprojection loads them into `health.ontology_categories`, `health.ontology_relations`, `health.clinical_types` and `health.concepts`, and builds `OntologyCategory`, `ClinicalType` and `Concept` vertices linked by `INSTANCE_OF`, `DEFAULT_CLINICAL_TYPE`, `SUBTYPE_OF`, `CONCEPT_CLINICAL_TYPE` and `REPRESENTS_CONCEPT`. Query them with `healthdb ontology` (`--types`, `--relations`), `healthdb concepts` and `healthdb concept ID`.
