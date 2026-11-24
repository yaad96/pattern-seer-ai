/**
 * Jersey HD Studio: the Google Sheet log. Script version 2 (2026-11-24).
 *
 * The app sends one row for each event: a job's end, tokens added or taken back, and a bKash payment sent,
 * approved or rejected. This script writes every row once into the log tab, and copies each checked bKash payment
 * (approved or rejected) into a second tab, "bKash".
 *
 * To put it in, or to update it:
 *   1. In the sheet: Extensions > Apps Script. Select everything in the editor, delete it, paste this whole text, Save.
 *   2. Deploy > Manage deployments > the pencil (Edit) > Version: "New version" > Deploy.
 *      The web app's address stays the same, so nothing changes in the app.
 * (A first set-up instead uses Deploy > New deployment > Web app, "Execute as: Me", "Who has access: Anyone".)
 *
 * Keep this text private: the line below is the password the app uses to write here.
 */
const SECRET = __SECRET__;
const VERSION = 2;
const LOG_TAB = 'Log'; // the log tab's name in a sheet that has none yet (an existing log tab is found by its header)
const PAY_TAB = 'bKash';
const KEEP = 20000; // a Row ID is looked for in this many of the newest rows
const PAY_RESULTS = { 'bKash payment approved': 'Approved', 'bKash payment rejected': 'Rejected' };
// The bKash tab's columns: [its heading, the log column it is copied from]
const PAY_COLUMNS = [
  ['Checked (Bangladesh)', 'Time (Bangladesh)'], ['Customer', 'Customer'], ['Result', 'Result'],
  ['Taka', 'bKash package taka'], ['Tokens added', 'Tokens added'], ['TrxID', 'bKash TrxID'],
  ['Sent from', 'bKash sender'], ['Checked by', 'Checked by'], ['Reason', 'Reason'],
  ['Tokens left after', 'Tokens left after'], ['Sent (Bangladesh)', 'bKash sent (Bangladesh)'],
  ['Package tokens', 'bKash package tokens'], ['Client ID', 'Client ID'], ['Machine', 'Machine'], ['Row ID', 'Row ID'],
];
// Always kept as text, exactly as sent: a phone number keeps its first 0, and a TrxID of digits stays as it is.
const TEXT_COLUMNS = { 'bKash TrxID': true, 'bKash sender': true, 'Client ID': true, TrxID: true, 'Sent from': true };

function doPost(e) {
  var body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    return answer({ ok: false, error: 'not JSON' });
  }
  if (!body || typeof body !== 'object' || body.secret !== SECRET) return answer({ ok: false, error: 'wrong secret' });
  var lock = LockService.getScriptLock();
  try {
    lock.waitLock(30000);
  } catch (err) {
    return answer({ ok: false, error: 'busy' });
  }
  try {
    return answer(write(body));
  } catch (err) {
    return answer({ ok: false, error: String((err && err.message) || err) });
  } finally {
    lock.releaseLock();
  }
}

/** Opened in a browser, the web app only says which script answers (no data, nothing written). */
function doGet() {
  return answer({ ok: true, app: 'Jersey HD Studio sheet log', version: VERSION });
}

function answer(data) {
  return ContentService.createTextOutput(JSON.stringify(data)).setMimeType(ContentService.MimeType.JSON);
}

function write(body) {
  var book = SpreadsheetApp.getActiveSpreadsheet();
  var columns = Array.isArray(body.columns) ? body.columns.map(String) : [];
  var rows = Array.isArray(body.rows) ? body.rows : [];
  var log = logTab(book);
  var header = headerOf(log, columns);
  var have = idsIn(log, header);
  var fresh = [];
  var added = [];
  rows.forEach(function (row) {
    if (!row || typeof row !== 'object') return;
    var id = String(row['Row ID'] || '');
    if (!id) return;
    added.push(id); // delivered, whether written now or already here
    if (have[id]) return;
    have[id] = true;
    fresh.push(header.map(function (name) { return cell(row[name], name); }));
  });
  if (fresh.length) log.getRange(log.getLastRow() + 1, 1, fresh.length, header.length).setValues(fresh);
  var payments = syncPayments(book, log, header, rows);
  return { ok: true, version: VERSION, written: fresh.length, added: added, payments: payments, sheet_url: book.getUrl() };
}

/** The log tab: the one whose first row has the log's headings (whatever it is called), else a new one. */
function logTab(book) {
  var sheets = book.getSheets();
  for (var i = 0; i < sheets.length; i++) {
    var sheet = sheets[i];
    if (sheet.getName() === PAY_TAB || sheet.getLastRow() < 1 || sheet.getLastColumn() < 1) continue;
    var first = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0].map(String);
    if (first.indexOf('Row ID') >= 0 && first.indexOf('Event') >= 0) return sheet;
  }
  return book.getSheetByName(LOG_TAB) || book.insertSheet(LOG_TAB);
}

/** A tab's headings, with any of `columns` it lacks added at the end; the heading row is bold and stays in view. */
function headerOf(sheet, columns) {
  var width = sheet.getLastColumn();
  var header = sheet.getLastRow() >= 1 && width >= 1 ? sheet.getRange(1, 1, 1, width).getValues()[0].map(String) : [];
  while (header.length && header[header.length - 1] === '') header.pop();
  var missing = columns.filter(function (name) { return header.indexOf(name) < 0; });
  if (missing.length) {
    sheet.getRange(1, header.length + 1, 1, missing.length).setValues([missing]).setFontWeight('bold');
    header = header.concat(missing);
  }
  if (header.length && sheet.getFrozenRows() < 1) sheet.setFrozenRows(1);
  return header;
}

/** The Row IDs in a tab's newest KEEP rows. */
function idsIn(sheet, header) {
  var col = header.indexOf('Row ID') + 1;
  var last = sheet.getLastRow();
  var have = {};
  if (col < 1 || last < 2) return have;
  var first = Math.max(2, last - KEEP + 1);
  sheet.getRange(first, col, last - first + 1, 1).getValues().forEach(function (r) {
    if (r[0] !== '') have[String(r[0])] = true;
  });
  return have;
}

/** A value as its cell: numbers stay numbers; text never becomes a formula (a client's message may start with =). */
function cell(value, column) {
  if (value === null || value === undefined) return '';
  if (typeof value === 'number' || typeof value === 'boolean' || value instanceof Date) return value;
  var text = typeof value === 'string' ? value : JSON.stringify(value);
  if (text === '') return '';
  return TEXT_COLUMNS[column] || /^[=+\-@]/.test(text) ? "'" + text : text;
}

/**
 * The bKash tab: one row for each payment that was checked. It is filled from the log tab, so a payment whose log
 * row arrived before this script was put in is copied too, and no payment is copied twice (by its Row ID).
 * It runs when the tab is new, or when rows just sent include a checked payment.
 */
function syncPayments(book, log, header, rows) {
  var tab = book.getSheetByName(PAY_TAB);
  var isNew = !tab;
  var wanted = isNew || rows.some(function (row) { return row && PAY_RESULTS[row.Event]; });
  if (!wanted) return 0;
  if (isNew) tab = book.insertSheet(PAY_TAB, book.getSheets().length);
  var payHeader = headerOf(tab, PAY_COLUMNS.map(function (pair) { return pair[0]; }));
  var eventCol = header.indexOf('Event') + 1;
  var idCol = header.indexOf('Row ID') + 1;
  var last = log.getLastRow();
  if (eventCol < 1 || idCol < 1 || last < 2) return 0;
  var first = Math.max(2, last - KEEP + 1);
  var count = last - first + 1;
  var events = log.getRange(first, eventCol, count, 1).getValues();
  var ids = log.getRange(first, idCol, count, 1).getValues();
  var have = idsIn(tab, payHeader);
  var from = {};
  PAY_COLUMNS.forEach(function (pair) { from[pair[0]] = pair[1]; });
  var out = [];
  for (var i = 0; i < count; i++) {
    var result = PAY_RESULTS[String(events[i][0])];
    var id = String(ids[i][0]);
    if (!result || !id || have[id]) continue;
    have[id] = true;
    var values = log.getRange(first + i, 1, 1, header.length).getValues()[0];
    var row = { Result: result };
    header.forEach(function (name, j) { if (name !== '' && !(name in row)) row[name] = values[j]; });
    out.push(payHeader.map(function (name) { return cell(row[from[name] || name], name); }));
  }
  if (out.length) tab.getRange(tab.getLastRow() + 1, 1, out.length, payHeader.length).setValues(out);
  return out.length;
}
