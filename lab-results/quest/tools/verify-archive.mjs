import fs from 'node:fs/promises';
import crypto from 'node:crypto';
import assert from 'node:assert/strict';

const root = new URL('../', import.meta.url);
const read = async path => JSON.parse(await fs.readFile(new URL(path, root), 'utf8'));
const hash = data => crypto.createHash('sha256').update(data).digest('hex');
const inventory = await read('inventory.json');
const queue = await read('resume-queue.json');
const survey = await read('listing-snapshots/final-history-survey.json');
const count = rows => {
  const counts = new Map();
  for (const row of rows) {
    const key = JSON.stringify([row.date, row.tests]);
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  return counts;
};
const observed = new Map();
for (const stage of survey.stages) {
  for (const [key, n] of count(stage.observed_rows)) observed.set(key, Math.max(n, observed.get(key) || 0));
}
const expected = count(queue.rows.map(row => ({date: row.listed_service_date_source, tests: row.list_text.split('\n')[1]})));
assert.deepEqual([...observed].sort(), [...expected].sort(), 'Final portal listing differs from queue');
assert.equal(survey.stages.at(-1).next_page_disabled, true);
assert.equal(survey.stages.at(-1).recent_empty, true);
const keys = new Set(inventory.reports.map(r => r.report_id));
assert.equal(keys.size, inventory.reports.length, 'Duplicate current report keys');
for (const row of queue.rows) {
  assert(keys.has(row.report_id));
  assert.equal(row.pdf_status, 'saved');
  assert(['saved', 'saved_pdf'].includes(row.result_archive_status));
}
let pages = 0, revisions = 0, portalReports = 0;
for (const report of inventory.reports) {
  const bytes = await fs.readFile(new URL(report.pdf_path, root));
  assert.equal(bytes.subarray(0, 5).toString(), '%PDF-');
  assert.equal(hash(bytes), report.pdf_sha256);
  const envelope = await read(report.pdf_envelope_path);
  assert.equal(envelope.report_id, report.report_id);
  assert.equal(envelope.pdf_sha256, report.pdf_sha256);
  assert.equal(envelope.pages.length, report.pdf_pages);
  for (const [index, page] of envelope.pages.entries()) {
    assert.equal(page.page_number, index + 1);
    assert(page.text.trim());
  }
  pages += envelope.pages.length;
  for (const revision of report.pdf_revisions || []) {
    assert.equal(hash(await fs.readFile(new URL(revision.pdf_path, root))), revision.pdf_sha256);
    revisions++;
  }
  if (report.archive_path) {
    const portal = await read(report.archive_path);
    const fingerprint = hash(JSON.stringify({header: portal.header_text, rows: portal.rows.map(({markup, ...row}) => row)}));
    assert.equal(fingerprint, report.portal_content_sha256);
    assert.equal(portal.collapsed_notes_remaining, 0);
    portalReports++;
  }
}
const exported = (await fs.readFile(new URL('reports.jsonl', root), 'utf8')).trim().split('\n').map(JSON.parse);
assert.deepEqual(exported.map(r => r.report_id).sort(), [...keys].sort());
assert.equal(new Set(exported.map(r => r.report_id)).size, exported.length);
console.log(JSON.stringify({listing_entries: queue.rows.length, unique_reports: keys.size, current_pdfs: inventory.reports.length, current_pdf_pages: pages, additional_pdf_versions: revisions, portal_archives: portalReports, missing_results: 0, duplicate_current_keys: 0, final_pagination_verified: true}));
