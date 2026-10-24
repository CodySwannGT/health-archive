import fs from 'node:fs/promises';
import crypto from 'node:crypto';
import path from 'node:path';

// Run through cua_repl with its supported tab handle; no credentials or network APIs.
export async function captureReport(tab, inventory, i, root) {
  const report = inventory.reports[i];
  if (await tab.url() !== report.source_url) {
    await tab.goto(report.source_url);
    await tab.getAXState({emit: false});
  }
  await tab.playwright.locator(`#result-detail a[href*="id=${report.report_id}&"]`).first().waitFor({state: 'visible', timeoutMs: 15000});
  const data = await tab.playwright.evaluate(() => {
    const root = document.querySelector('#result-detail');
    const visible = el => el.getClientRects().length > 0;
    return {
      text: root.innerText,
      rendered_markup: root.outerHTML,
      sections: Array.from(root.querySelectorAll('.panel-summary,.result-panel')).filter(visible).map(el => ({
        kind: el.classList.contains('panel-summary') ? 'panel' : 'result',
        heading: el.querySelector('h5')?.innerText || null,
        text: el.innerText,
        markup: el.outerHTML,
      })),
      notes_count: Array.from(root.querySelectorAll('a')).filter(el => visible(el) && el.innerText === 'Result Notes').length,
      linked_report_ids: Array.from(root.querySelectorAll('a[href*="/results/trend?"]')).map(el => new URL(el.href).searchParams.get('id')),
    };
  });
  const [y,m,d] = (report.listed_date || report.service_date).split('-');
  if (!data.text.includes(`${m}/${d}/${y}`) || data.linked_report_ids.some(id => id !== report.report_id)) throw Error('Report identity mismatch');
  const notes = [];
  for (let n=0; n<data.notes_count; n++) {
    await tab.playwright.getByRole('link', {name: 'Result Notes', exact: true}).nth(n).click();
    await tab.getAXState({emit: false});
    const note = await tab.playwright.evaluate(() => Array.from(document.querySelectorAll('.modal-content')).filter(el => el.getClientRects().length).map(el => ({
      title: el.querySelector('h4')?.innerText,
      text: el.querySelector('.modal-body')?.innerText,
    }))[0]);
    if (!note?.title?.startsWith('Result Notes')) throw Error('Notes missing');
    notes.push(note);
    await tab.playwright.getByRole('button', {name: 'Close', exact: true}).click();
    await tab.getAXState({emit: false});
  }
  const digest = crypto.createHash('sha256').update(JSON.stringify({text: data.text, notes})).digest('hex');
  const base = `labcorp_${report.patient_id}_${report.report_id}`;
  const archivePath = `portal-reports/${base}.json`;
  const textPath = `portal-reports/${base}.txt`;
  const now = new Date().toISOString();
  let existing;
  try { existing = JSON.parse(await fs.readFile(path.join(root,archivePath),'utf8')); } catch(e) { if(e.code !== 'ENOENT') throw e; }
  const changed = existing?.portal_content_sha256 !== digest;
  if (existing && changed) {
    await fs.mkdir(path.join(root,'revisions'), {recursive: true});
    const priorPath = path.join(root,'revisions',`${base}_${existing.portal_content_sha256}.json`);
    try { await fs.writeFile(priorPath, JSON.stringify(existing,null,2)+'\n', {flag:'wx'}); } catch(e) { if(e.code !== 'EEXIST') throw e; }
  }
  const dateLabel = data.text.includes('Purchase Date:') ? 'purchase_date' : 'service_date';
  const listedDate = report.listed_date || report.service_date;
  Object.assign(report, {
    status: 'portal_archived', official_pdf_status: 'blocked_browser_blob_url',
    portal_content_sha256: digest, archive_path: archivePath, text_path: textPath,
    result_cards: data.sections.filter(s => s.kind === 'result').length,
    notes_count: notes.length, portal_date_label: dateLabel, last_checked_at: now,
    listed_date: listedDate,
    service_date: dateLabel === 'service_date' ? listedDate : null,
    purchase_date: dateLabel === 'purchase_date' ? listedDate : null,
    report_date: data.text.split('Report Date: ')[1]?.split('\n')[0] || null,
  });
  if (changed) {
    const archive = {schema_version:'1.0', ...report, captured_at:now, archive_kind:'portal_rendered_results', ...data, notes};
    await fs.writeFile(path.join(root,archivePath),JSON.stringify(archive,null,2)+'\n');
    await fs.writeFile(path.join(root,textPath),data.text+'\n\nSUPPLEMENTAL RESULT NOTES\n'+notes.map(n => n.title+'\n'+n.text).join('\n\n')+'\n');
  }
  await fs.writeFile(path.join(root,'inventory.json'),JSON.stringify(inventory,null,2)+'\n');
  return {index:i+1,report_id:report.report_id,results:report.result_cards,notes:report.notes_count,changed};
}
