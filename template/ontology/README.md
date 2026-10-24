# Ontology

The ontology the health database enforces. `categories.json` registers every resource category, its domain and the JSON Schema that validates its records. `relations.json` registers every graph relationship type and its evidence semantics. Reprojection fails when a resource or relationship uses an unregistered entry, so a new distinction must be added here first.

These files are database-owned archive files. Change them by editing a local copy and running `healthdb put ontology/FILE`. Reprojection loads them into `health.ontology_categories` and `health.ontology_relations`, adds an `OntologyCategory` vertex for each category, and links each resource with `INSTANCE_OF`. Query them with `healthdb ontology`.
