"""Fail if a repository tree could expose personal health information or secrets.

    python privacy/scan.py [ROOT] [--deny FILE]

Generic checks run everywhere: archive data paths, data file types, email
addresses, patient and user identifiers, password hashes, keys and connection
strings. A private deny-list (one case-insensitive term per line, kept outside
the public repository) adds checks for an operator's own identifying terms.
Allowed exceptions are exact strings in specific files, listed in ALLOWED.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

ARCHIVE_DIRS = ('database/artifacts/', 'lab-results/', 'health-records/', 'vitals/', 'research/', 'conversations/', 'checks/', 'ontology/', 'workspace/', 'symptoms/', 'meds/', 'identity/local-profile.json')
# Code that lives inside archive directories may be published.
CODE_DIRS = ('symptoms/tools/', 'meds/tools/', 'lab-results/tools/', 'lab-results/quest/tools/', 'health-records/cleveland-clinic/tools/', 'vitals/oneblood/tools/', 'research/tools/')
FORBIDDEN_SUFFIXES = ('.pdf', '.xlsx', '.xls', '.csv', '.dump', '.jsonl', '.zip', '.age', '.key', '.pem', '.crt', '.db', '.sqlite')
PATTERNS = {
    'email address': re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'),
    'patient or user identifier': re.compile(r'\b(?:pat|usr)_[0-9a-f]{32}\b'),
    'password hash': re.compile(r'scrypt\$\d+\$\d+\$\d+\$[A-Za-z0-9_-]{16,}'),
    # Built from parts so this file does not match its own patterns.
    'private key': re.compile('-----' + 'BEGIN [A-Z ]*PRIVATE ' + 'KEY-----'),
    'certificate': re.compile('-----' + 'BEGIN ' + 'CERTIFICATE-----'),
    'connection string with password': re.compile(r'postgres(?:ql)?://[^:\s/]+:[^@\s]+@'),
    'cloud access key': re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b'),
}
ALLOWED_EMAILS = re.compile(r'^(?:noreply@anthropic\.com|dev@example\.com|[^@]+@example\.(?:com|org|test))$')
ALLOWED_IDS = {'pat_00000000000000000000000000000001', 'usr_00000000000000000000000000000001'}
# Publisher attribution is public: deny-list terms are ignored only in these manifest fields.
PUBLISHER_MANIFESTS = ('plugins/health-archive/plugin.json', 'plugins/health-archive/.claude-plugin/plugin.json', '.claude-plugin/marketplace.json')
PUBLISHER_KEYS = ('author', 'owner', 'developerName')


def publisher_values(text):
    values = []
    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in PUBLISHER_KEYS:
                    values.extend([value] if isinstance(value, str) else [v for v in value.values() if isinstance(v, str)] if isinstance(value, dict) else [])
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(json.loads(text))
    return values


def files(root):
    git = subprocess.run(['git', '-C', str(root), 'ls-files', '--cached', '--others', '--exclude-standard'], capture_output=True, text=True)
    if git.returncode == 0:
        return sorted(p for p in git.stdout.splitlines() if (root / p).is_file())
    return sorted(str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() and '.git' not in p.parts)


def scan(root, deny_terms=()):
    findings = []
    for rel in files(root):
        lower = rel.lower()
        if lower.startswith(ARCHIVE_DIRS) and not lower.startswith(CODE_DIRS):
            findings.append((rel, 'archive data path'))
            continue
        if lower.endswith(FORBIDDEN_SUFFIXES):
            findings.append((rel, 'data or secret file type'))
            continue
        try:
            text = (root / rel).read_text()
        except UnicodeDecodeError:
            continue
        for name, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                value = match.group(0)
                if name == 'email address' and ALLOWED_EMAILS.match(value):
                    continue
                if name == 'patient or user identifier' and value in ALLOWED_IDS:
                    continue
                findings.append((rel, f'{name}: {value[:12]}…'))
        scrubbed = text
        if rel in PUBLISHER_MANIFESTS:
            for value in publisher_values(text):
                scrubbed = scrubbed.replace(value, '')
        for term in deny_terms:
            if re.search(r'(?<![a-z0-9])' + re.escape(term) + r'(?![a-z0-9])', scrubbed, re.I):
                findings.append((rel, 'private deny-list term'))
    return findings


def main():
    cli = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    cli.add_argument('root', nargs='?', default='.', type=Path)
    cli.add_argument('--deny', type=Path, help='Private file of case-insensitive terms, one per line')
    args = cli.parse_args()
    terms = [t.strip() for t in args.deny.read_text().splitlines() if t.strip() and not t.startswith('#')] if args.deny else []
    findings = scan(args.root.resolve(), terms)
    for rel, reason in findings:
        # Never print the matched private term itself.
        print(f'{rel}: {reason}')
    print(f'{len(findings)} finding(s) in {args.root}')
    sys.exit(1 if findings else 0)


if __name__ == '__main__':
    main()
