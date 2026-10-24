import fs from 'node:fs/promises';
import crypto from 'node:crypto';
import path from 'node:path';
import {sourceIdentity} from './native-capture.mjs';

export async function captureCurrent(tab, listRow, root) {
  const frame = tab.playwright.frameLocator('iframe[title="Lab results detail"]');
  await frame.locator('#myContainerStart').filter({hasText:listRow.list_text.split('\n')[0]}).waitFor({state:'visible',timeoutMs:15000});
  let expanded = 0;
  while(await frame.locator('.showmore').filter({visible:true}).count()) {
    await frame.locator('.showmore').filter({visible:true}).first().click();
    await tab.getAXState({emit:false});
    if(++expanded>150) throw Error('Notes expansion limit');
  }
  const header = await frame.locator('#myContainerStart').innerText();
  const results = await frame.locator('#report-tests').evaluate(el => {
    const visible = n => n.getClientRects().length > 0;
    return {
      text:el.innerText,
      rendered_markup:el.outerHTML,
      rows:Array.from(el.querySelectorAll('[id="labRowTable"]')).filter(n => visible(n) && n.querySelector('.analyteValue') && n.querySelector('.displayReportName')?.textContent.trim() !== 'Analyte').map((n,ordinal) => ({
        ordinal,
        analyte_name:n.querySelector('.displayReportName')?.textContent.trim() || null,
        result_text:n.querySelector('.analyteValue')?.innerText.trim() || null,
        reference_text:n.querySelector('.unitContainer')?.innerText.trim() || null,
        source_loinc_link_id:n.querySelector('.loincAnchor')?.id || null,
        notes:Array.from(n.querySelectorAll('.obxNote')).filter(visible).map(note => note.innerText),
        raw_text:n.innerText,
        markup:n.outerHTML,
      })),
      collapsed_notes_remaining:Array.from(el.querySelectorAll('.showmore')).filter(visible).length,
    };
  });
  const field = name => header.match(new RegExp('(?:^|\\n)'+name+':[\\t ]*([^\\n]*)'))?.[1].trim() || null;
  const specimen=field('Specimen'), requisition=field('Requisition'), collected=field('Collected');
  if(!specimen || !requisition || !collected || results.collapsed_notes_remaining) throw Error('Report metadata or notes incomplete');
  const listedDate=listRow.list_text.split('\n')[0];
  if(![collected,field('Received'),field('Reported')].some(date => date?.startsWith(listedDate))) throw Error('Report date does not match the listed collection, receipt, or reporting date');
  const dateParts=collected.split(' ')[0].split('/');
  const collectionDate=`${dateParts[2]}-${dateParts[0]}-${dateParts[1]}`;
  const reportId=`${specimen}_${requisition}_${collectionDate}`;
  const fingerprint=crypto.createHash('sha256').update(JSON.stringify({header,rows:results.rows.map(({markup,...r})=>r)})).digest('hex');
  const record={schema_version:'1.0',source:'quest',report_id:reportId,specimen_id:specimen,requisition_id:requisition,patient_id:field('Patient ID'),patient_name:(await sourceIdentity()).quest_printed_name,listed_service_date_source:listedDate,collection_date:collectionDate,collected_at_source:collected,received_at_source:field('Received'),reported_at_source:field('Reported'),report_status:field('Report Status'),source_url:await tab.url(),captured_at:new Date().toISOString(),archive_kind:'portal_rendered_results',portal_content_sha256:fingerprint,header_text:header,...results};
  const base='quest_'+reportId;
  const archivePath='portal-reports/'+base+'.json';
  let existing;
  try{existing=JSON.parse(await fs.readFile(path.join(root,archivePath),'utf8'));}catch(e){if(e.code!=='ENOENT')throw e;}
  const changed=existing?.portal_content_sha256!==fingerprint;
  if(existing && changed){
    await fs.mkdir(path.join(root,'revisions'),{recursive:true});
    try{await fs.writeFile(path.join(root,'revisions',base+'_'+existing.portal_content_sha256+'.json'),JSON.stringify(existing,null,2)+'\n',{flag:'wx'});}catch(e){if(e.code!=='EEXIST')throw e;}
  }
  if(changed){
    await fs.writeFile(path.join(root,archivePath),JSON.stringify(record,null,2)+'\n');
    await fs.writeFile(path.join(root,'portal-reports',base+'.txt'),header+'\n'+results.text+'\n');
  }
  return {record,archive_path:archivePath,text_path:'portal-reports/'+base+'.txt',changed,existing,expanded};
}
