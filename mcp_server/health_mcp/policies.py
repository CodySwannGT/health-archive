"""The privacy policy and terms the connector serves at /privacy and /terms.

Each statement describes what the code in this repository does. Change the text
when the behavior changes, and bump UPDATED.
"""
import os

UPDATED = 'November 7, 2026'
OPERATOR = os.environ.get('HEALTH_MCP_OPERATOR', 'Cody Swann')
CONTACT = os.environ.get('HEALTH_MCP_CONTACT', 'cody.swann@gmail.com')

PRIVACY = f'''<p>Last updated {UPDATED}. Health Archive is run by {OPERATOR}. Questions and requests: <a href="mailto:{CONTACT}">{CONTACT}</a>.</p>

<h2>What Health Archive is</h2>
<p>A private archive of your own health records that you reach through an AI assistant such as Claude, ChatGPT or Codex.
It stores what you tell your assistant to record and answers questions from those records. It is not a medical provider,
does not give medical advice, and is not a covered entity under HIPAA.</p>

<h2>What we collect</h2>
<ul>
<li><strong>Account:</strong> your username, the name you give, your time zone and your password, stored only as a scrypt hash.</li>
<li><strong>Health information you record:</strong> symptoms, medications and doses, measurements such as weight and blood pressure, sleep,
exercise, lab results and documents you add, your check-in answers, and the messages of the conversations your assistant saves to the archive
as their source. Each record keeps the exact words it came from.</li>
<li><strong>Connection data:</strong> the name and return address each assistant registers when it connects, and sign-in codes and tokens,
stored only as SHA-256 hashes.</li>
<li><strong>Security records:</strong> failed sign-ins (username and time), kept for 15 minutes; a SHA-256 hash of the network address of each sign-up, kept for one hour
to limit sign-ups.</li>
<li><strong>Server logs:</strong> our hosting provider records each request's network address, path and status, as server logs do.</li>
</ul>
<p>We do not use cookies, analytics, advertising or tracking.</p>

<h2>How we use it</h2>
<p>Only to run the archive for you: to store your records, answer your assistant's questions about them, find patterns in your own records,
and keep the service secure. We never sell your data, use it for advertising, or use it to train AI models.</p>

<h2>Who can see it</h2>
<ul>
<li><strong>You,</strong> through the assistants you connect. Each sign-in is bound to your account, and database row-level security limits every
read to your own records.</li>
<li><strong>The AI assistant you connect</strong> receives the records it asks for on your behalf. Its own privacy policy covers what it does with them.</li>
<li><strong>Our service providers:</strong> Railway hosts the server and database, and daily backups are encrypted before they are stored in a private storage bucket.
Search embeddings are computed on our own server; no other service receives your health information.</li>
<li><strong>The operator</strong> can access the database to run and repair the service, and does so only for that purpose.</li>
<li><strong>Legal requests:</strong> we disclose data only when the law requires it.</li>
</ul>

<h2>Pooled patterns (off unless you turn it on)</h2>
<p>If you opt in, the patterns found in your records, such as a medication tracking a symptom, are combined with those of other people who opted in.
A pattern is shown only when at least five people contribute, and pooled patterns contain no names, records or identifiers. You can turn pooling off at any time.</p>

<h2>How long we keep it</h2>
<p>Your archive is kept until you delete it. Records are never silently changed: corrections are stored alongside the original so the history stays intact.</p>

<h2>Deleting your account</h2>
<p>Delete your account and archive at any time at <a href="/account/delete">/account/delete</a>. Deletion is immediate and permanent: it removes your account,
every record, document and saved conversation, and signs out every connected assistant. Encrypted backups made before the deletion can still contain your data
until those backups are deleted. Email <a href="mailto:{CONTACT}">{CONTACT}</a> if you cannot sign in and want your account deleted.</p>

<h2>Your choices and rights</h2>
<p>You can ask your assistant to show any record and its source, correct a record, or turn pooling on or off, and email us for a full copy of your data. Depending on where you live,
you may have further rights over your health data, such as to know, access, correct or delete it, or to withdraw consent. Email us to exercise any of them.</p>

<h2>Security</h2>
<p>Connections use TLS. Passwords, sign-in codes and tokens are stored only as hashes, repeated failed sign-ins lock the account temporarily, and backups are
encrypted. No system is perfectly secure; if a breach affects your data we will notify you as the law requires.</p>

<h2>Children</h2>
<p>Health Archive is not for anyone under 18, and we do not knowingly collect their data.</p>

<h2>Changes</h2>
<p>We will post any change here with a new date.</p>
'''

TERMS = f'''<p>Last updated {UPDATED}. Health Archive is run by {OPERATOR}. Contact: <a href="mailto:{CONTACT}">{CONTACT}</a>.</p>

<h2>Not medical advice</h2>
<p>Health Archive stores and organizes your health records and finds patterns in them. Patterns are associations in your own records, not causes or diagnoses.
Nothing from Health Archive or an assistant using it is medical advice, diagnosis or treatment. Talk to a qualified clinician before changing a medication or making a
health decision. In an emergency, call your local emergency number.</p>

<h2>Your account</h2>
<p>You must be 18 or older. Keep your password private; you are responsible for what is recorded through your account. Record only your own health information,
or information you have the right to record.</p>

<h2>Your data</h2>
<p>Your records stay yours. You give us permission to store and process them only to run the service for you, as the <a href="/privacy">privacy policy</a> describes.
You can delete your account at any time.</p>

<h2>Acceptable use</h2>
<p>Do not try to reach another person's records, overload or attack the service, or use it to break the law.</p>

<h2>The service</h2>
<p>Health Archive is provided as is, without warranties, and may change or stop. Keep your own copies of records that matter to you.
To the extent the law allows, the operator is not liable for indirect or consequential damages, or for decisions made using the service.</p>

<h2>Changes</h2>
<p>We will post changes here with a new date. Continuing to use the service after a change means you accept it.</p>
'''
