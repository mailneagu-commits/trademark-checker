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
  const cls = { "Înregistrată": "ok", "Cerere publicată": "pub", "Admisă la înregistrare": "adm", "Respinsă": "bad" }[s] || "pub";
  return s ? `<span class="st ${cls}">${esc(s)}</span>` : "";
}
const chips = cl => `<div class="chips">${(cl || []).map(c => `<span class="chip">${c}</span>`).join("")}</div>`;
const thumb = r => r.image ? `<img class="thumb" src="${esc(r.image)}" alt="" loading="lazy">` : `<div class="thumb empty">—</div>`;
const markLink = r => `/marca/${encodeURIComponent(r.app_number)}`;

function header(active) {
  const links = [["/", "Căutare", "home"], ["/recente.html", "Cereri recente", "recente"], ["/clasament.html", "Clasament", "clasament"]];
  document.body.insertAdjacentHTML("afterbegin", `
    <header class="top"><div class="wrap">
      <a class="brand" href="/" aria-label="${SITE.name} — prima pagină"><img src="/prowatch-logo.png" alt="${SITE.name}" width="160" height="40"><small>${SITE.tagline}</small></a>
      <nav class="nav">${links.map(([h, t, k]) => `<a href="${h}" class="${k === active ? "on" : ""}">${t}</a>`).join("")}
        <a href="#" class="cta" onclick="openLead('cercetare');return false;">Solicită o cercetare</a></nav>
    </div></header>`);
  document.body.insertAdjacentHTML("beforeend", `
    <footer><div class="wrap fgrid">
      <img src="/protectmark-logo.png" alt="ProtectMark — intellectual property attorneys" width="110" height="64">
      <div><div><strong style="color:var(--ink);">${esc(SITE.name)}</strong> este un serviciu ProtectMark. Datele provin din Buletinul Oficial de Proprietate Industrială (BOPI), secțiunea Mărci, publicat de OSIM.</div>
      <div class="small" style="margin-top:6px;">Informațiile au caracter orientativ și nu înlocuiesc o cercetare de disponibilitate făcută de un consilier în proprietate industrială. Pentru situația juridică oficială a unei mărci consultați OSIM.</div></div>
    </div></footer>
    <div class="leadbar" id="leadbar"><span id="leadcount"></span><button class="btn alt" onclick="openLead('info')">Solicită informații</button></div>
    <div class="modal" id="leadModal" onclick="if(event.target===this)closeLead()"><div class="card">
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
    </div></div>`);
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
  document.getElementById("leadcount").textContent = `${picked.size} ${picked.size === 1 ? "marcă selectată" : "mărci selectate"}`;
}
let leadKind = "info";
function openLead(kind) {
  leadKind = kind;
  document.getElementById("leadTitle").textContent = kind === "cercetare" ? "Solicită o cercetare de disponibilitate" : "Solicită informații despre mărci";
  document.getElementById("leadIntro").textContent = kind === "cercetare"
    ? "Spuneți-ne denumirea pe care vreți s-o înregistrați și clasele de produse/servicii — vă trimitem o cercetare de anterioritate."
    : (picked.size ? `Mărcile selectate: ${[...picked].join(", ")}. Vă trimitem situația juridică actuală și datele titularului.` : "Spuneți-ne despre ce marcă doriți informații.");
  document.getElementById("msgLabel").textContent = kind === "cercetare" ? "Denumirea mărcii, clasele, detalii" : "Mesaj";
  document.getElementById("leadMsg").innerHTML = "";
  document.getElementById("leadModal").classList.add("on");
}
function closeLead() { document.getElementById("leadModal").classList.remove("on"); }
async function sendLead(e) {
  e.preventDefault();
  const f = new FormData(e.target), msg = document.getElementById("leadMsg");
  try {
    await api("/api/lead", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: leadKind, name: f.get("name"), email: f.get("email"), phone: f.get("phone"),
                             message: f.get("message"), marks: leadKind === "info" ? [...picked] : [] }) });
    msg.innerHTML = `<div class="note ok">Mulțumim! Am primit solicitarea și revenim în cel mai scurt timp.</div>`;
    e.target.reset();
    if (leadKind === "info") { picked.clear(); sessionStorage.removeItem("picked"); updateLeadbar();
      document.querySelectorAll("input.pick").forEach(c => c.checked = false); }
  } catch (err) { msg.innerHTML = `<div class="note err">${esc(err.message)}</div>`; }
}

// ── Tabel de rezultate (căutare, recente) ────────────────────────────────────
function resultsTable(rows) {
  if (!rows.length) return `<div class="empty-state">Niciun rezultat.</div>`;
  return `<div class="tbl-wrap"><table class="tbl"><thead><tr><th></th><th></th><th>Marcă</th><th>Nr. depozit</th><th>Titular</th><th>Clase</th><th>Stare</th><th>Data depozit</th></tr></thead><tbody>
    ${rows.map(r => `<tr>
      <td><input type="checkbox" class="pick" ${picked.has(r.app) ? "checked" : ""} onchange="togglePick('${esc(r.app)}', this.checked)" aria-label="Selectează ${esc(r.name)}"></td>
      <td class="img"><a href="${markLink(r)}">${thumb(r)}</a></td>
      <td><a class="mname" href="${markLink(r)}">${esc(r.name || "(figurativă)")}</a><div class="muted small">${esc(r.mark_type)}</div></td>
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
        ${["", "Înregistrată", "Cerere publicată", "Admisă la înregistrare", "Respinsă"].map(s =>
          `<option value="${s}" ${qs.get("status") === s ? "selected" : ""}>${s || "toate"}</option>`).join("")}</select></label>
    </div>
  </form>`;
}
