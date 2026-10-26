// idmate.js — shared UI helpers (FIXES 6.6, foundation for 12.7)
// Globally available on all pages via _base.html (<script defer>).
// Deliberately NO IIFE/module encapsulation: simple global function declarations
// are idempotent — if the file (or a page with a local copy) defines them twice,
// the last declaration wins, without runtime errors. Since this file runs via
// defer AFTER the inline scripts, it overrides any local copies identically
// (same semantics) — no behavior changes.

// ── HTML escaper ──────────────────────────────────────────────
// Converts & < > " ' into HTML entities; non-string/empty input -> ''.
function escapeHtml(s) {
  if (!s) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// Alias so that pages already using esc() stay compatible.
function esc(s) {
  return escapeHtml(s);
}

// ── Toast CSS (self-injecting, idempotent) ────────────────────
// Injects the toast styling ONCE so that toast() works on every page without
// an idmate.css change. id guard prevents double injection.
// Taken 1:1 from admin.html (.toast-container/.toast/.error/.success +
// prefers-reduced-motion).
function _idmateInjectToastCss() {
  if (document.getElementById('idmate-toast-css')) return;
  var style = document.createElement('style');
  style.id = 'idmate-toast-css';
  style.textContent =
    '.toast-container{position:fixed;left:50%;bottom:1.2rem;transform:translateX(-50%);z-index:11000;display:flex;flex-direction:column;gap:0.5rem;align-items:center;max-width:92vw;pointer-events:none}' +
    '.toast{pointer-events:auto;background:var(--bg-card);border:1px solid var(--border);border-left:4px solid var(--accent);border-radius:8px;padding:0.6rem 0.9rem;color:var(--text-1);font-size:0.85rem;line-height:1.35;box-shadow:0 6px 20px rgba(0,0,0,0.45);max-width:420px;word-wrap:break-word;opacity:0;transform:translateY(12px);transition:opacity .25s ease,transform .25s ease}' +
    '.toast.show{opacity:1;transform:translateY(0)}' +
    '.toast.error{border-left-color:var(--danger)}' +
    '.toast.success{border-left-color:var(--success-btn)}' +
    '@media (prefers-reduced-motion: reduce){.toast{transform:none;transition:opacity .15s linear}.toast.show{transform:none}}';
  (document.head || document.documentElement).appendChild(style);
}

// ── Toast ─────────────────────────────────────────────────────
// Non-blocking notification (replaces alert()).
// type: 'error' (red), 'success' (green), otherwise neutral/info (blue).
// Empty/undefined message -> nothing. textContent => XSS-safe.
function toast(message, type) {
  if (message === undefined || message === null || message === '') return;
  _idmateInjectToastCss();
  var container = document.getElementById('toastContainer');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toastContainer';
    container.className = 'toast-container';
    document.body.appendChild(container);
  }
  var el = document.createElement('div');
  el.className = 'toast' + (type === 'error' ? ' error' : type === 'success' ? ' success' : '');
  el.setAttribute('role', type === 'error' ? 'alert' : 'status');
  el.setAttribute('aria-live', type === 'error' ? 'assertive' : 'polite');
  el.textContent = String(message);
  container.appendChild(el);
  // Force a reflow so the fade-in transition takes effect.
  requestAnimationFrame(function () {
    requestAnimationFrame(function () { el.classList.add('show'); });
  });
  var remove = function () {
    el.classList.remove('show');
    setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 300);
  };
  setTimeout(remove, 3000);
}

// ── confirmModal CSS (self-injecting, idempotent) ─────────────
// Own, prefixed classes (idmate-confirm-*) so the modal looks the same on EVERY
// page — independent of page-specific or idmate.css .modal/.btn-*.
// (idmate.css already defines .modal/.btn-danger, but differently from the
// admin.html template; hence deliberately encapsulated classes instead of reuse.)
// id guard prevents double injection. Dark-theme tokens + prefers-reduced-motion.
function _idmateInjectConfirmCss() {
  if (document.getElementById('idmate-confirm-css')) return;
  var style = document.createElement('style');
  style.id = 'idmate-confirm-css';
  style.textContent =
    '.idmate-confirm-bg{position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:12000;display:none;justify-content:center;align-items:center}' +
    '.idmate-confirm-bg.open{display:flex}' +
    '.idmate-confirm{background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:1.2rem;max-width:450px;width:90%;box-shadow:0 8px 28px rgba(0,0,0,.5);transform:translateY(8px);transition:transform .18s ease}' +
    '.idmate-confirm-bg.open .idmate-confirm{transform:translateY(0)}' +
    '.idmate-confirm p{font-size:0.9rem;color:var(--text-1);line-height:1.4;margin:0 0 0.8rem}' +
    '.idmate-confirm .idmate-confirm-btns{display:flex;gap:0.5rem;justify-content:flex-end;margin-top:0.6rem}' +
    '.idmate-confirm .idmate-confirm-btns button{padding:0.4rem 0.8rem;border-radius:6px;border:1px solid var(--border);font-size:0.85rem;cursor:pointer}' +
    '.idmate-confirm .idmate-confirm-cancel{background:var(--bg-hover);color:var(--text-1)}' +
    '.idmate-confirm .idmate-confirm-cancel:hover{background:var(--bg-active)}' +
    '.idmate-confirm .idmate-confirm-ok{background:var(--danger-btn);border-color:var(--danger);color:#fff}' +
    '.idmate-confirm .idmate-confirm-ok:hover{background:var(--danger)}' +
    '.idmate-confirm .idmate-confirm-ok:focus-visible,.idmate-confirm .idmate-confirm-cancel:focus-visible{outline:2px solid var(--accent);outline-offset:2px}' +
    '@media (prefers-reduced-motion: reduce){.idmate-confirm{transform:none;transition:none}.idmate-confirm-bg.open .idmate-confirm{transform:none}}';
  (document.head || document.documentElement).appendChild(style);
}

// ── confirmModal ──────────────────────────────────────────────
// Promise-based replacement for the blocking native confirm().
// Call pattern: `if (!await confirmModal(msg)) return;`
//   OK / Enter            -> resolve(true)
//   Cancel / Escape       -> resolve(false)
//   Backdrop click        -> resolve(false)  (drift-safe: mousedown AND click)
// Self-contained: lazily creates its DOM once (reused afterwards) and
// appends it to document.body — works without a page-specific #confirmModal.
// Button labels are i18n via window.IDMATE_LABELS.ok / .cancel (fallback DE).
// Message via textContent => XSS-safe. On the admin page this (defer) version
// overrides the local one, with identical true/false semantics.
var _idmateConfirmRefs = null;
function _idmateBuildConfirm() {
  if (_idmateConfirmRefs) return _idmateConfirmRefs;
  _idmateInjectConfirmCss();
  var bg = document.createElement('div');
  bg.className = 'idmate-confirm-bg';
  bg.setAttribute('role', 'dialog');
  bg.setAttribute('aria-modal', 'true');
  var box = document.createElement('div');
  box.className = 'idmate-confirm';
  var msg = document.createElement('p');
  var btns = document.createElement('div');
  btns.className = 'idmate-confirm-btns';
  var cancelBtn = document.createElement('button');
  cancelBtn.className = 'idmate-confirm-cancel';
  cancelBtn.type = 'button';
  var okBtn = document.createElement('button');
  okBtn.className = 'idmate-confirm-ok';
  okBtn.type = 'button';
  btns.appendChild(cancelBtn);
  btns.appendChild(okBtn);
  box.appendChild(msg);
  box.appendChild(btns);
  bg.appendChild(box);
  document.body.appendChild(bg);
  _idmateConfirmRefs = { bg: bg, msg: msg, okBtn: okBtn, cancelBtn: cancelBtn };
  return _idmateConfirmRefs;
}

function confirmModal(message) {
  return new Promise(function (resolve) {
    var r = _idmateBuildConfirm();
    var bg = r.bg, okBtn = r.okBtn, cancelBtn = r.cancelBtn;
    var labels = window.IDMATE_LABELS || {};
    okBtn.textContent = labels.ok || 'OK';
    cancelBtn.textContent = labels.cancel || 'Abbrechen';
    r.msg.textContent = String(message == null ? '' : message);
    r.msg.style.whiteSpace = 'pre-line';   // \n in messages as a line break
    bg.classList.add('open');
    okBtn.focus();
    var done = false;
    var downOnSelf = false;
    var finish = function (result) {
      if (done) return; done = true;
      bg.classList.remove('open');
      okBtn.removeEventListener('click', onOk);
      cancelBtn.removeEventListener('click', onCancel);
      bg.removeEventListener('mousedown', onDown);
      bg.removeEventListener('click', onBg);
      document.removeEventListener('keydown', onKey);
      resolve(result);
    };
    var onOk = function () { finish(true); };
    var onCancel = function () { finish(false); };
    // Backdrop click = cancel, drift-safe (mousedown AND click on bg)
    var onDown = function (e) { downOnSelf = (e.target === bg); };
    var onBg = function (e) { if (e.target === bg && downOnSelf) finish(false); };
    var onKey = function (e) {
      if (e.key === 'Escape') finish(false);
      else if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    };
    okBtn.addEventListener('click', onOk);
    cancelBtn.addEventListener('click', onCancel);
    bg.addEventListener('mousedown', onDown);
    bg.addEventListener('click', onBg);
    document.addEventListener('keydown', onKey);
  });
}

// ── CSP-safe event delegation (FIXES 7.2) ─────────────────────
// Replaces inline handler attributes (onclick= etc.) that a CSP without
// 'unsafe-inline' blocks. Markup convention:
//   <button data-onclick="fnName" data-args='[1,"x"]'>
// The dispatcher calls window.fnName.call(element, ...args, event) —
// `this` is, like with an inline handler, the element; the event is appended
// as the LAST extra argument (functions without an event parameter ignore it).
// data-args is a JSON array (optional). No eval/Function needed.
(function () {
  function dispatch(ev, attr) {
    var el = ev.target && ev.target.closest && ev.target.closest('[' + attr + ']');
    if (!el) return;
    var name = el.getAttribute(attr);
    var fn = window[name];
    if (typeof fn !== 'function') {
      console.error('idmate-delegate: window.' + name + ' is not a function (' + attr + ')');
      return;
    }
    var args = [];
    var raw = el.getAttribute('data-args');
    if (raw) {
      try { args = JSON.parse(raw); }
      catch (e) { console.error('idmate-delegate: data-args is not JSON:', raw); return; }
    }
    fn.apply(el, args.concat([ev]));
  }
  // Bubble phase for events that bubble (same ordering semantics as
  // inline handlers); capture only for focus/blur (which do not bubble).
  ['click', 'change', 'input', 'keydown', 'submit', 'mouseover', 'mousedown'].forEach(function (t) {
    document.addEventListener(t, function (ev) { dispatch(ev, 'data-on' + t); }, false);
  });
  ['focus', 'blur'].forEach(function (t) {
    document.addEventListener(t, function (ev) { dispatch(ev, 'data-on' + t); }, true);
  });
})();

// Small named helper for what used to be inline "event.stopPropagation()":
// <span data-onclick="stopProp"> — the event comes in as the last argument.
function stopProp(ev) { ev.stopPropagation(); }

/* Drucken in der iOS-Home-Bildschirm-App (standalone): window.print()
   liefert dort seit Jahren keinen brauchbaren Druckdialog – und seit iOS 26
   öffnet jede zum Home-Bildschirm gelegte Seite als Web-App. In Safari zu
   springen (1.2.127) war keine Lösung: fremdes Fenster, zweite Anmeldung.
   Jetzt bleibt alles in der App: Sie macht selbst ein PDF aus der
   gezeichneten Seite (html2pdf = html2canvas + jsPDF, static/vendor) und
   reicht es dem Teilen-Menü von iOS – dort stehen Drucken (AirPrint), In
   Dateien sichern, Mail. Charts kommen mit (html2canvas übernimmt die
   Canvas-Pixel). Für das Rendern wird die Seite kurz auf A4-Innenbreite
   gesetzt, damit das PDF am Handy aussieht wie der Ausdruck am Rechner
   (pdfFromElement).

   Zwei Tipper, nicht einer: Das Teilen-Menü darf iOS nur direkt aus einer
   Berührung öffnen, das Rendern dauert aber Sekunden. Deshalb baut der erste
   Tipp das PDF (Knopf: „PDF wird erstellt …" → „Teilen / Drucken"), der
   zweite reicht es weiter. Seit 1.2.184 läuft auch der Druck am Rechner und
   in jedem Browser über das PDF (der Browserdruck kam mit den Charts nicht
   mehr klar): dort ein Klick, das PDF wird gespeichert. */
function iosStandalone() { return window.navigator.standalone === true; }

/* Wo das PDF in ein Teilen-Menü geht (Handy, Tablet, iOS-App): zwei Tipper,
   weil das Menü nur direkt aus einer Berührung aufgehen darf. Am Rechner
   ein Klick: PDF bauen und als Datei speichern. */
function pdfTeilen() {
  if (iosStandalone()) return true;
  if (!(window.matchMedia && matchMedia('(pointer: coarse)').matches)) return false;
  try {
    return !!(navigator.canShare && navigator.canShare({ files: [new File([''], 'x.pdf', { type: 'application/pdf' })] }));
  } catch (e) { return false; }
}

/* Gedruckt wird überall über das PDF: der Browserdruck kommt mit den Charts
   nicht mehr zurecht. window.print() bleibt nur, wenn html2pdf fehlt. */
function pdfDruck() { return !!window.html2pdf; }

/* A4 hoch mit 12 mm Rand links/rechts: 186 mm Inhalt = 703 px bei 96 dpi.
   Für das Rendern bekommt die Seite genau diese Breite – die Tabellen legen
   sich wie am Rechner, und Chart.js zeichnet seine Diagramme in dieser
   Breite neu (statt Handy-Bilder auf A4 zu strecken). Danach alles zurück. */
const PDF_CONTENT_PX = 703;

async function pdfSwapCanvases(el) {
  const swaps = [];
  for (const c of el.querySelectorAll('canvas')) {
    if (!c.clientWidth || !c.clientHeight) continue;
    let img;
    try { img = new Image(); img.src = c.toDataURL('image/png'); } catch (e) { continue; }
    img.style.cssText = 'display:block;width:' + c.clientWidth + 'px;height:' + c.clientHeight + 'px';
    img.className = c.className;
    try { await img.decode(); } catch (e) { /* Bild ist trotzdem gesetzt */ }
    c.parentNode.insertBefore(img, c); c.style.display = 'none';
    swaps.push([c, img]);
  }
  return swaps;
}

/* Graustufen fuer das PDF: html2canvas kennt kein CSS filter, deshalb
   werden Bilder mit data-pdf-gray (img oder background-image) fuer die
   Dauer des Renderns durch eine graue Kopie ersetzt. */
const _pdfGrayCache = {};
async function pdfGrayUrl(url) {
  if (_pdfGrayCache[url]) return _pdfGrayCache[url];
  const img = new Image();
  img.crossOrigin = 'anonymous';
  img.src = url;
  await img.decode();
  const c = document.createElement('canvas');
  c.width = img.naturalWidth; c.height = img.naturalHeight;
  const ctx = c.getContext('2d');
  ctx.drawImage(img, 0, 0);
  const d = ctx.getImageData(0, 0, c.width, c.height), px = d.data;
  for (let i = 0; i < px.length; i += 4) {
    const y = 0.299 * px[i] + 0.587 * px[i + 1] + 0.114 * px[i + 2];
    px[i] = px[i + 1] = px[i + 2] = y;
  }
  ctx.putImageData(d, 0, 0);
  return (_pdfGrayCache[url] = c.toDataURL('image/png'));
}
async function pdfSwapGray(el) {
  const swaps = [];
  for (const e of el.querySelectorAll('[data-pdf-gray]')) {
    try {
      if (e.tagName === 'IMG') {
        const gray = await pdfGrayUrl(e.currentSrc || e.src);
        const orig = e.src;
        swaps.push(() => { e.src = orig; });
        e.src = gray;
      } else {
        const bg = getComputedStyle(e).backgroundImage;
        const m = /url\(["']?(.*?)["']?\)/.exec(bg);
        if (!m) continue;
        const gray = await pdfGrayUrl(m[1]);
        const orig = e.style.backgroundImage;
        swaps.push(() => { e.style.backgroundImage = orig; });
        e.style.backgroundImage = 'url("' + gray + '")';
      }
    } catch (err) { /* Bild bleibt farbig */ }
  }
  return swaps;
}

async function pdfFromElement(el) {
  const body = document.body, html = document.documentElement;
  const prev = { width: body.style.width, minWidth: body.style.minWidth, overflowX: body.style.overflowX };
  body.style.width = PDF_CONTENT_PX + 'px'; body.style.minWidth = PDF_CONTENT_PX + 'px'; body.style.overflowX = 'hidden';
  html.classList.add('pdf-rendering');
  const charts = window.Chart && window.Chart.instances ? Object.values(window.Chart.instances) : [];
  const nextFrame = () => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
  let swaps = [], grays = [];
  try {
    await nextFrame();
    charts.forEach(c => { try { c.resize(); c.update('none'); } catch (e) { /* egal */ } });
    await nextFrame(); await new Promise(r => setTimeout(r, 120));
    // html2pdf klont Canvas als <img> in Bitmap-Groesse (bei Chart.js
    // devicePixelRatio 2 also doppelt so gross -> beschnitten). Deshalb
    // die Canvas selbst kurz durch ein Bild in CSS-Groesse ersetzen.
    swaps = await pdfSwapCanvases(el);
    grays = await pdfSwapGray(el);
    return await html2pdf().set({
      margin: [12, 12, 14, 12],
      image: { type: 'jpeg', quality: 0.95 },
      html2canvas: { scale: 2, useCORS: true, backgroundColor: '#ffffff', windowWidth: PDF_CONTENT_PX + 40,
                     scrollX: 0, scrollY: -window.scrollY },
      jsPDF: { unit: 'mm', format: 'a4', orientation: 'portrait' },
      pagebreak: { mode: ['css', 'legacy'], before: '.page-break-before' },
    }).from(el).outputPdf('blob');
  } finally {
    swaps.forEach(([c, img]) => { img.remove(); c.style.display = ''; });
    grays.forEach(restore => restore());
    body.style.width = prev.width; body.style.minWidth = prev.minWidth; body.style.overflowX = prev.overflowX;
    html.classList.remove('pdf-rendering');
    await nextFrame();
    charts.forEach(c => { try { c.resize(); c.update('none'); } catch (e) { /* egal */ } });
  }
}

function pdfDownload(file) {
  const url = URL.createObjectURL(file);
  const a = document.createElement('a');
  a.href = url; a.download = file.name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

async function pdfShareFile(file) {
  if (navigator.canShare && navigator.canShare({ files: [file] })) {
    // Nur die Datei, kein title/text: iOS haengt die sonst als Text an
    // (Mail, Nachrichten, Notizen bekamen den Dateinamen als Inhalt).
    try { await navigator.share({ files: [file] }); return; }
    catch (e) { if (e && e.name === 'AbortError') return; }
  }
  // Ohne Teilen-Menü: als Datei anbieten.
  pdfDownload(file);
}

/* el: der zu druckende Bereich; filename: Vorschlag fürs PDF; btn: der
   Knopf, der Zustand und Beschriftung trägt (data-pdf-*-Texte). */
async function printOrShare(el, filename, btn) {
  if (!pdfDruck()) { window.print(); return; }
  if (!pdfTeilen()) {
    // Rechner: ein Klick, das PDF landet als Datei (Druck aus dem PDF-Programm).
    if (btn && btn.dataset.pdfState === 'busy') return;
    const label = btn ? btn.textContent : '';
    if (btn) { btn.dataset.pdfState = 'busy'; btn.textContent = btn.dataset.pdfBusy || 'PDF …'; }
    try {
      const blob = await pdfFromElement(el);
      pdfDownload(new File([blob], filename, { type: 'application/pdf' }));
    } catch (e) {
      if (typeof toast === 'function') toast((btn && btn.dataset.pdfError) || 'PDF: ' + e, 'error');
    } finally {
      if (btn) { btn.dataset.pdfState = ''; btn.textContent = label; }
    }
    return;
  }
  if (btn && btn.dataset.pdfState === 'ready' && btn._pdfFile) {
    await pdfShareFile(btn._pdfFile);
    return;
  }
  if (btn && btn.dataset.pdfState === 'busy') return;
  const label = btn ? btn.textContent : '';
  if (btn) { btn.dataset.pdfState = 'busy'; btn.textContent = btn.dataset.pdfBusy || 'PDF …'; }
  try {
    const blob = await pdfFromElement(el);
    const file = new File([blob], filename, { type: 'application/pdf' });
    if (btn) { btn._pdfFile = file; btn.dataset.pdfState = 'ready'; btn.textContent = btn.dataset.pdfShare || 'PDF'; }
    else await pdfShareFile(file);
  } catch (e) {
    if (btn) { btn.dataset.pdfState = ''; btn.textContent = label; }
    if (typeof toast === 'function') toast((btn && btn.dataset.pdfError) || 'PDF: ' + e, 'error');
  }
}
