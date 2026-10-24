import fs from 'node:fs/promises';

import os from 'node:os';
import {fileURLToPath} from 'node:url';

export const root=fileURLToPath(new URL('..', import.meta.url)).replace(/\/$/,'');
// The patient's printed name on provider portals comes from the local profile's source_identity.
export async function sourceIdentity() {
  const file=process.env.HEALTH_PROFILE||os.homedir()+'/.config/health-archive/local-profile.json';
  const profile=JSON.parse(await fs.readFile(file,'utf8'));
  return profile.patients.find(p=>p.patient_id===profile.default_patient_id).source_identity||{};
}
export function listRows(ax) {
  return [...ax.matchAll(/^\s*\d+ text (?:Date of service Lab tests (?:Status )?)?(\d{2}\/\d{2}\/\d{4}) ([^\n]+)\n[\s\S]*?^\s*(\d+) button View result details$/gm)]
    .map((m,index)=>({index,date:m[1],tests:m[2],button:Number(m[3])}));
}
function button(ax,label) {
  const m=ax.match(new RegExp('^\\s*(\\d+) button '+label+'$','m'));
  if(!m)throw Error('Missing button: '+label);
  return Number(m[1]);
}
async function observe(app,test,label) {
  for(let i=0;i<5;i++){
    const ax=await app.getAXState({disableDiffing:true,emit:false});
    if(test(ax))return ax;
  }
  throw Error('UI is not ready: '+label);
}
export async function openRow(app,date,occurrence=0) {
  const ax=await observe(app,s=>listRows(s).some(r=>r.date===date),'listing');
  const row=listRows(ax).filter(r=>r.date===date)[occurrence];
  if(!row)throw Error('Listing occurrence missing');
  await app.click(row.button);
  await app.getAXState({emit:false});
  return row;
}
export async function prepareExport(app,row) {
  let ax=await observe(app,s=>s.includes('container Lab results detail')&&s.includes('Specimen:'),'report');
  const specimen=ax.match(/Specimen:[ \t]*([A-Z0-9]+)/)?.[1];
  if(!specimen||!ax.includes(row.date))throw Error('Report identity mismatch');
  const url=ax.match(/(?:URL: |Value: )(myquest\.questdiagnostics\.com\/results\/\S+)/)?.[1];
  let pdfTab=false;
  if(!/^\s*\d+ button Download$/m.test(ax)) {
    const print=ax.match(/^\s*(\d+) text Print$/m);
    if(!print)throw Error('Print control missing');
    await app.click(Number(print[1]));
    ax=await observe(app,s=>s.includes('text Simple Lab results only'),'export menu');
    await app.click(Number(ax.match(/^\s*(\d+) text Simple Lab results only$/m)[1]));
    await app.getAXState({emit:false});
    pdfTab=true;
  }
  return {source:'quest',date:row.date,tests:row.tests,specimen_id:specimen,
    filename:'quest_'+row.date.slice(6)+'-'+row.date.slice(0,2)+'-'+row.date.slice(3,5)+'_'+specimen+'.pdf',
    source_url:url?'https://'+url:null,pdfTab};
}
export async function finishSave(app,meta) {
  let ax=await observe(app,s=>s.includes('saveAsNameTextField'),'save dialog');
  const field=ax.match(/^\s*(\d+) text field[^\n]*ID: saveAsNameTextField/m);
  await app.setValue(Number(field[1]),meta.filename);
  if(!ax.includes('Value: pdfs'))throw Error('Choose archive folder before calling finishSave again');
  await app.click(button(ax,'Save, ID: OKButton'));
  await observe(app,s=>!s.includes('saveAsNameTextField'),'save completion');
  await fs.access(root+'/pdfs/'+meta.filename);
  const receipt={...meta,captured_at:new Date().toISOString(),status:'saved'};
  await fs.mkdir(root+'/download-receipts',{recursive:true});
  await fs.writeFile(root+'/download-receipts/'+meta.filename+'.json',JSON.stringify(receipt,null,2)+'\n');
  await app.pressKey('Escape');
  if(meta.pdfTab){
    await app.pressKey('super+w');
    ax=await observe(app,s=>s.includes('container Lab results detail'),'source report');
  }else ax=await app.getAXState({disableDiffing:true,emit:false});
  const backs=[...ax.matchAll(/^\s*(\d+) button Back$/gm)];
  if(!backs.length)throw Error('Source report Back missing');
  await app.click(Number(backs.at(-1)[1]));
  await app.getAXState({emit:false});
  return receipt;
}
export async function download(app,meta) {
  const ax=await observe(app,s=>/^\s*\d+ button Download$/m.test(s)&&s.includes('Specimen:'),'PDF');
  if(!ax.includes(meta.specimen_id)||!ax.includes(meta.date))throw Error('PDF identity mismatch');
  try{await fs.access(root+'/pdfs/'+meta.filename);throw Error('PDF already exists; reconcile instead of overwriting');}
  catch(e){if(e.code!=='ENOENT')throw e;}
  await app.click(button(ax,'Download'));
  return finishSave(app,meta);
}
