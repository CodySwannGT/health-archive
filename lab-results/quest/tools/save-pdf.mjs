import fs from 'node:fs/promises';
import crypto from 'node:crypto';

// Only operates on the observed HTTPS Quest PDF and its native Save dialog.
export async function savePdf(pdf, app, root, filename) {
  const url=await pdf.url();
  if(!url.startsWith('https://ii.questdiagnostics.com/ii-render-web/app/ws/pdf/labreport.pdf'))throw Error('Unexpected PDF destination');
  let state=await app.getAXState({emit:false,disableDiffing:true});
  if(!state.includes(url.replace(/^https:\/\//,'')))throw Error('Unexpected active Chrome window');
  const index=(pattern)=>{const match=state.match(pattern);if(!match)throw Error('Required native control missing');return Number(match[1]);};
  await app.click(index(/^\s*(\d+) button Download\s*$/m));
  state=await app.getAXState({emit:false,disableDiffing:true});
  await app.setValue(index(/^\s*(\d+) text field \(settable\).*ID: saveAsNameTextField/m),filename);
  if(!state.includes('Value: pdfs, ID: where popup')) {
    await app.pressKey('super+shift+g');
    state=await app.getAXState({emit:false,disableDiffing:true});
    await app.setValue(index(/^\s*(\d+) text field \(settable\).*ID: PathTextField/m),root+'/pdfs');
    await app.pressKey('Return');
    state=await app.getAXState({emit:false,disableDiffing:true});
  } else state=await app.getAXState({emit:false,disableDiffing:true});
  await app.click(index(/^\s*(\d+) button Save, ID: OKButton/m));
  await app.getAXState({emit:false,disableDiffing:true});
  const bytes=await fs.readFile(root+'/pdfs/'+filename);
  if(bytes.subarray(0,5).toString()!=='%PDF-')throw Error('Invalid PDF file');
  return {pdf_path:'pdfs/'+filename,pdf_sha256:crypto.createHash('sha256').update(bytes).digest('hex'),pdf_bytes:bytes.length};
}
