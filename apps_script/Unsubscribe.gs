/**
 * One-click unsubscribe endpoint. Bound to the outreach Google Sheet, so it
 * writes straight into the Suppression tab - the pipeline checks that tab
 * before every send.
 *
 * Setup (README "Unsubscribe link"):
 *  1. In the sheet: Extensions > Apps Script, paste this file.
 *  2. Project Settings > Script Properties: add UNSUBSCRIBE_SECRET (same value as the GitHub secret).
 *  3. Deploy > New deployment > Web app. Execute as: Me. Who has access: Anyone.
 *  4. Put the /exec URL into the UNSUBSCRIBE_BASE_URL secret.
 *
 * Links look like  <exec-url>?e=<email>&t=<hmac>. The token stops anyone from
 * unsubscribing addresses they don't own; it is HMAC-SHA256(email) truncated to 32 hex chars.
 */

function verify_(email, token) {
  var secret = PropertiesService.getScriptProperties().getProperty('UNSUBSCRIBE_SECRET');
  if (!secret || !token) return false;
  var sig = Utilities.computeHmacSha256Signature(email, secret);
  var hex = sig.map(function (b) { return ('0' + (b & 0xff).toString(16)).slice(-2); }).join('');
  return hex.substring(0, 32) === String(token);
}

function suppress_(email) {
  var lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    var sh = SpreadsheetApp.getActive().getSheetByName('Suppression');
    var last = sh.getLastRow();
    var existing = last > 1 ? sh.getRange(2, 1, last - 1, 1).getValues().map(function (r) { return String(r[0]).toLowerCase(); }) : [];
    if (existing.indexOf(email) === -1) {
      sh.appendRow([email, 'email', 'unsubscribe link', 'unsubscribe link', new Date().toISOString()]);
    }
  } finally {
    lock.releaseLock();
  }
}

function handle_(params) {
  var email = String(params.e || '').trim().toLowerCase();
  if (!email || !verify_(email, params.t)) return false;
  suppress_(email);
  return true;
}

// Link clicked in the email.
function doGet(req) {
  var ok = handle_(req.parameter || {});
  var msg = ok
    ? 'You\'re unsubscribed. You won\'t receive any more emails from Hamid Ali Asad (hamidbuilds.dev). Sorry for the interruption.'
    : 'This unsubscribe link is invalid. Reply "unsubscribe" to the email and you\'ll be removed by hand.';
  return HtmlService.createHtmlOutput(
    '<div style="font-family:sans-serif;max-width:520px;margin:60px auto;line-height:1.5">' + msg + '</div>');
}

// RFC 8058 one-click (Gmail/Yahoo "Unsubscribe" button) POSTs here.
function doPost(req) {
  handle_(req.parameter || {});
  return ContentService.createTextOutput('ok');
}
