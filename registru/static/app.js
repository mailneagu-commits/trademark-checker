// Comun tuturor paginilor: antet, subsol, utilitare, formularul „Solicită informații”.
const SITE = { name: "ProWatch", tagline: "Registrul mărcilor OSIM<br>un serviciu ProtectMark" };

const esc = t => String(t ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const dmy = iso => { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || ""); return m ? `${m[3]}.${m[2]}.${m[1]}` : ""; };
const fmt = n => Number(n || 0).toLocaleString("ro-RO");
const qs = new URLSearchParams(location.search);

async function api(path, opts) {
  const r = await fetch(path, opts);
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || `Eroare ${r.status}`);
  return d;
}

function statusBadge(s) {
  const cls = { "Înregistrată": "ok", "Cerere publicată": "pub", "Admisă la înregistrare": "adm", "Respinsă": "bad",
                "Radiată": "bad", "Retrasă": "bad", "Expirată": "bad" }[s] || "pub";
  return s ? `<span class="st ${cls}">${esc(s)}</span>` : "";
}
const chips = cl => `<div class="chips">${(cl || []).map(c => `<span class="chip">${c}</span>`).join("")}</div>`;
const thumb = r => r.image ? `<img class="thumb" src="${esc(r.image)}" alt="" loading="lazy">` : `<div class="thumb empty">—</div>`;
const markLink = r => `/marca/${encodeURIComponent(r.app_number)}`;

// Mod încorporat (?embed=1): pagina rulează într-un iframe pe protectmark.ro — fără antet și
// subsol proprii, linkurile păstrează modul, iar înălțimea e trimisă paginii-gazdă.
const EMBED = window.self !== window.top && (qs.get("embed") === "1" || sessionStorage.getItem("pwEmbed") === "1");
if (EMBED) {
  try { sessionStorage.setItem("pwEmbed", "1"); } catch (e) {}
  document.documentElement.classList.add("embed");
  const sendHeight = () => parent.postMessage({ prowatchHeight: document.documentElement.scrollHeight }, "*");
  window.addEventListener("load", sendHeight);
  new ResizeObserver(sendHeight).observe(document.documentElement);
  document.addEventListener("click", e => {
    const a = e.target.closest && e.target.closest("a[href]");
    if (!a || a.target || a.origin !== location.origin) return;
    const u = new URL(a.href);
    if (!u.searchParams.has("embed")) { u.searchParams.set("embed", "1"); a.href = u.toString(); }
  }, true);
  document.addEventListener("submit", e => {
    if (e.target.method === "get" && !e.target.querySelector("input[name=embed]"))
      e.target.insertAdjacentHTML("beforeend", '<input type="hidden" name="embed" value="1">');
  }, true);
}

// Bara „Solicită informații” și fereastra de solicitare — în ambele moduri (site / încorporat).
const LEAD_UI = `
    <div class="leadbar" id="leadbar"><span id="leadcount"></span>
      <button class="btn alt" onclick="openLead('info')">Solicită informații</button>
      <button class="btn ghost" style="color:#fff;border-color:rgba(255,255,255,.5);padding:8px 12px;" onclick="clearPicked()" title="Golește selecția">✕ Golește</button></div>
    <div class="modal" id="leadModal" onclick="if(event.target===this)closeLead()"><div class="card" style="position:relative;">
      <button type="button" class="lead-x" onclick="closeLead()" aria-label="Închide">✕</button>
      <div id="leadThanks" class="lead-thanks" style="display:none;">Vă mulțumim pentru solicitare! Veți fi sunat de un consultant ProtectMARK în cel mai scurt timp.<br><strong>Protect your ideas! Protect your business! Protect your future!</strong></div>
      <div id="leadBody">
      <h2 id="leadTitle">Solicită informații</h2>
      <p class="muted small" id="leadIntro"></p>
      <form class="form-grid" onsubmit="sendLead(event)">
        <div><label>Nume *</label><input name="name" required maxlength="200"></div>
        <div><label>Email *</label><input name="email" type="email" required maxlength="200"></div>
        <div><label>Telefon</label><input name="phone" maxlength="50"></div>
        <div><label id="msgLabel">Mesaj</label><textarea name="message" rows="4" maxlength="4000"></textarea></div>
        <div id="leadMsg"></div>
        <div style="display:flex;gap:8px;justify-content:flex-end;"><button type="button" class="btn ghost" onclick="closeLead()">Renunță</button><button class="btn">Trimite</button></div>
        <p class="muted small" style="margin:0;">Datele trimise sunt folosite doar pentru a vă răspunde la această solicitare.</p>
      </form>
      </div>
    </div></div>`;

function header(active) {
  if (EMBED) {
    // Încorporat în protectmark.ro: fără antet și subsol proprii (le are pagina-gazdă).
    document.body.insertAdjacentHTML("beforeend", LEAD_UI);
    return;
  }
  const links = [["/", "Căutare", "home"], ["/recente.html", "Cereri recente", "recente"]];
  document.body.insertAdjacentHTML("afterbegin", `
    <header class="top"><div class="wrap">
      <a class="brand" href="/" aria-label="${SITE.name} — prima pagină"><img src="/prowatch-logo.png" alt="${SITE.name}" width="160" height="40"><small>${SITE.tagline}</small></a>
      <nav class="nav">${links.map(([h, t, k]) => `<a href="${h}" class="${k === active ? "on" : ""}">${t}</a>`).join("")}
        <a href="#" class="cta" onclick="openLead('cercetare');return false;">Verificare disponibilitate</a></nav>
    </div></header>`);
  document.body.insertAdjacentHTML("beforeend", `
    <footer><div class="wrap fgrid">
      <img src="/protectmark-logo.png" alt="ProtectMark — intellectual property attorneys" width="110" height="64">
      <div><div><strong>${esc(SITE.name)}</strong> este un serviciu ProtectMark. Datele provin din Buletinul Oficial de Proprietate Industrială (BOPI), secțiunea Mărci, publicat de OSIM.</div>
      <div class="small" style="margin-top:6px;">Informațiile au caracter orientativ și nu înlocuiesc verificarea disponibilității mărcii făcută de un consilier în proprietate industrială. Pentru situația juridică oficială a unei mărci <a href="#" onclick="openLead('info');return false;">solicitați o consultanță gratuită</a>.</div></div>
    </div></footer>
` + LEAD_UI);
}

// ── Mărci bifate + solicitare ────────────────────────────────────────────────
const picked = new Set(JSON.parse(sessionStorage.getItem("picked") || "[]"));
function togglePick(app, on) {
  on ? picked.add(app) : picked.delete(app);
  try { sessionStorage.setItem("picked", JSON.stringify([...picked])); } catch (e) {}
  updateLeadbar();
}
function updateLeadbar() {
  const bar = document.getElementById("leadbar");
  if (!bar) return;
  bar.classList.toggle("on", picked.size > 0);
  document.getElementById("leadcount").textContent =
    `${picked.size} ${picked.size === 1 ? "marcă selectată" : "mărci selectate"}: ${[...picked].join(", ")}`;
  document.querySelectorAll("input.pick").forEach(c => c.checked = picked.has(c.dataset.app));
}
function clearPicked() {
  picked.clear();
  try { sessionStorage.removeItem("picked"); } catch (e) {}
  updateLeadbar();
}
let leadKind = "info", leadMarks = null;
// marks: mărcile pentru această solicitare (ex. butonul de pe pagina unei mărci) — fără să modifice
// selecția din liste; dacă lipsește, se folosesc mărcile bifate.
function openLead(kind, marks) {
  leadKind = kind;
  leadMarks = marks || null;
  const list = leadMarks || [...picked];
  document.getElementById("leadTitle").textContent = kind === "cercetare" ? "Solicită verificarea disponibilității mărcii" : "Solicită informații despre mărci";
  document.getElementById("leadIntro").textContent = kind === "cercetare"
    ? "Spuneți-ne denumirea pe care vreți s-o înregistrați și clasele de produse/servicii — vă trimitem verificarea disponibilității mărcii."
    : (list.length ? `${list.length === 1 ? "Marca" : "Mărcile"}: ${list.join(", ")}. Vă trimitem situația juridică actuală și datele titularului.` : "Spuneți-ne despre ce marcă doriți informații.");
  document.getElementById("msgLabel").textContent = kind === "cercetare" ? "Denumirea mărcii, clasele, detalii" : "Mesaj";
  document.getElementById("leadMsg").innerHTML = "";
  document.getElementById("leadBody").style.display = "";
  document.getElementById("leadThanks").style.display = "none";
  document.getElementById("leadModal").classList.add("on");
}
function closeLead() { document.getElementById("leadModal").classList.remove("on"); leadMarks = null; }
async function sendLead(e) {
  e.preventDefault();
  const f = new FormData(e.target), msg = document.getElementById("leadMsg");
  const fromList = !leadMarks;
  const marks = leadKind === "info" ? (leadMarks || [...picked]) : [];
  try {
    await api("/api/lead", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: leadKind, name: f.get("name"), email: f.get("email"), phone: f.get("phone"),
                             message: f.get("message"), marks }) });
    // După trimitere formularul dispare și rămâne doar mesajul de mulțumire.
    document.getElementById("leadBody").style.display = "none";
    document.getElementById("leadThanks").style.display = "";
    e.target.reset();
    if (leadKind === "info" && fromList) clearPicked();
  } catch (err) { msg.innerHTML = `<div class="note err">${esc(err.message)}</div>`; }
}

// ── Tabel de rezultate (căutare, recente) ────────────────────────────────────
function resultsTable(rows) {
  if (!rows.length) return `<div class="empty-state">Niciun rezultat.</div>`;
  return `<div class="tbl-wrap"><table class="tbl"><thead><tr><th></th><th></th><th>Marcă</th><th>Nr. depozit</th><th>Titular</th><th>Clase</th><th>Stare</th><th>Data depozit</th></tr></thead><tbody>
    ${rows.map(r => `<tr>
      <td><input type="checkbox" class="pick" data-app="${esc(r.app)}" ${picked.has(r.app) ? "checked" : ""} onchange="togglePick('${esc(r.app)}', this.checked)" aria-label="Selectează ${esc(r.name)}"></td>
      <td class="img"><a href="${markLink(r)}">${thumb(r)}</a></td>
      <td><a class="mname" href="${markLink(r)}">${esc(r.name || (r.placeholder ? `(${r.placeholder})` : "(figurativă)"))}</a><div class="muted small">${esc(r.mark_type)}</div></td>
      <td style="white-space:nowrap;">${esc(r.app)}</td>
      <td>${esc(r.holder)}</td>
      <td>${chips(r.classes)}</td>
      <td>${statusBadge(r.status)}</td>
      <td style="white-space:nowrap;">${dmy(r.filing_date)}</td>
    </tr>`).join("")}</tbody></table></div>`;
}
document.addEventListener("DOMContentLoaded", updateLeadbar);

// ── Formularul de căutare (prima pagină și pagina de rezultate) ──────────────
function searchForm() {
  const v = k => esc(qs.get(k) || "");
  return `<form class="searchbox" action="/cauta.html" method="get">
    <div class="sgrid">
      <div><label for="f-name">Denumire marcă</label><input id="f-name" name="name" value="${v("name")}" placeholder="ex. MUSCLE SAUCE" autocomplete="off"></div>
      <div><label for="f-holder">Titular</label><input id="f-holder" name="holder" value="${v("holder")}" placeholder="ex. SC EXEMPLU SRL" autocomplete="off"></div>
      <div><label for="f-rep">Reprezentant</label><input id="f-rep" name="representative" value="${v("representative")}" placeholder="consilier / agenție" autocomplete="off"></div>
      <div><label for="f-cls">Clase Nisa</label><input id="f-cls" name="classes" value="${v("classes")}" placeholder="ex. 9, 35" inputmode="numeric"></div>
      <div class="go"><button class="btn alt" style="width:100%;">Caută</button></div>
    </div>
    <div class="sopts">
      <label><input type="radio" name="mode" value="contains" ${qs.get("mode") !== "similar" ? "checked" : ""}> conține textul</label>
      <label><input type="radio" name="mode" value="similar" ${qs.get("mode") === "similar" ? "checked" : ""}> denumiri asemănătoare</label>
      <label>Stare: <select name="status" style="width:auto;padding:4px 8px;">
        ${[["", "toate"], ["Valabile", "valabile (înregistrate, neexpirate)"], ["Înregistrată", "înregistrate"], ["Expirată", "expirate"],
           ["Cerere publicată", "cereri publicate"], ["Admisă la înregistrare", "admise la înregistrare"], ["Respinsă", "respinse"],
           ["Radiată", "radiate"], ["Retrasă", "retrase"]].map(([v, t]) =>
          `<option value="${v}" ${qs.get("status") === v ? "selected" : ""}>${t}</option>`).join("")}</select></label>
    </div>
  </form>`;
}

