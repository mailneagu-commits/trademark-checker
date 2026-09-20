/* Afișare mărci din buletinele OSIM/EUIPO — folosit de index.html (tabele) și mark.html (pagina unei mărci). */

function _bEsc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

// ── Blocuri reutilizabile pentru o marcă din buletin (OSIM/EUIPO) ─────────────
const _bD10 = v => (v ? String(v).slice(0, 10) : "");

// (731) Solicitant — nume + adresă (+ țară) per solicitant
function bulletinApplicantsHtml(m) {
  if ((m.applicants || []).length) {
    return m.applicants.map(a =>
      `<div style="margin-bottom:4px;"><strong>${_bEsc(a.name) || "—"}</strong>${a.address ? '<br><span style="font-size:.75rem;color:#777;">' + _bEsc(a.address) + '</span>' : ''}${a.country ? ' <span style="font-size:.72rem;color:#aaa;">(' + _bEsc(a.country) + ')</span>' : ''}</div>`
    ).join("");
  }
  const names = (m.applicantName || []).map(n => `<div><strong>${_bEsc(n)}</strong></div>`).join("");
  return (names + (m.applicantAddress ? `<div style="font-size:.75rem;color:#777;">${_bEsc(m.applicantAddress)}</div>` : "")) || "—";
}

// (740) Reprezentant — doar numele (fără adresă)
function bulletinRepsHtml(m) {
  const names = (m.representatives || [])
    .map(r => r.fullName || r.name || r.organizationName || "")
    .concat(m.representative ? [m.representative] : [])
    .map(n => String(n).trim()).filter(Boolean);
  const uniq = [...new Set(names)];
  return uniq.length ? uniq.map(n => `<div><strong>${_bEsc(n)}</strong></div>`).join("") : "—";
}

// (511) Clase descrise: fiecare clasă din depunere, cu titlul NISA, lista de
// produse/servicii a mărcii (când o avem) și descrierea generică a clasei.
function bulletinClassesHtml(m) {
  const goods = m.goodAndServices || [];
  const seen  = new Set();
  const block = (nc, short, text, desc) =>
    `<div class="goods-block" style="margin-bottom:6px;padding:7px 9px;">
       <div class="cls-title">Clasa ${_bEsc(nc)}${short ? " — " + _bEsc(short) : ""}</div>
       ${text ? `<div class="cls-text">${_bEsc(text)}</div>` : ""}
       ${desc ? `<div class="cls-desc">${_bEsc(desc)}</div>` : ""}
     </div>`;
  let html = (m.niceDetailed || []).map(nd => {
    seen.add(String(nd.class));
    const g = goods.filter(x => String(x.niceClassInt || x.niceClass) === String(nd.class));
    return g.length
      ? g.map(x => block(nd.class, nd.short, x.goodsAndServices, nd.description)).join("")
      : block(nd.class, nd.short, "", nd.description);
  }).join("");
  html += goods.filter(x => !seen.has(String(x.niceClassInt || x.niceClass)))
    .map(x => block(x.niceClass, x.niceShort, x.goodsAndServices, x.niceDescription)).join("");
  return html || '<span style="color:#aaa;">—</span>';
}

// (300) Prioritate revendicată — o linie per prioritate: țară · nr. cerere · dată
function bulletinPrioritiesHtml(m) {
  return (m.priorities || []).map(p => {
    const bits = [p.country, p.applicationNumber ? "nr. " + p.applicationNumber : "", _bD10(p.applicationDate)]
      .filter(Boolean).join(" · ");
    return _bEsc(bits + (p.partial ? " (parțială)" : ""));
  }).join("<br>");
}

// Senioritate invocată — o linie per senioritate: țară · tip · nr. · data înregistrării · data priorității
function bulletinSenioritiesHtml(m) {
  const kinds = { NATIONAL_REGISTRATION_IN_MEMBER_STATE: "înregistrare națională",
                  INTERNATIONAL_REGISTRATION_WITH_EFFECT_IN_MEMBER_STATE: "înregistrare internațională" };
  return (m.seniorities || []).map(q => {
    const kind = kinds[q.kind] || String(q.kind || "").toLowerCase().replace(/_/g, " ");
    const bits = [q.country, kind, q.registrationNumber || q.applicationNumber,
                  q.registrationDate ? "înreg. " + _bD10(q.registrationDate) : "",
                  q.priorityDate ? "prioritate " + _bD10(q.priorityDate) : ""].filter(Boolean).join(" · ");
    return _bEsc(bits + (q.partial ? " (parțială)" : ""));
  }).join("<br>");
}

// Restul datelor care nu au coloană proprie în tabel
function bulletinExtraFieldsHtml(m) {
  const item = (label, val) =>
    `<div class="detail-item"><label style="display:block;">${label}</label><span>${val ? val : "—"}</span></div>`;
  const opp    = (m.oppositionStartDate || m.oppositionEndDate)
    ? `${_bEsc(_bD10(m.oppositionStartDate)) || "?"} → ${_bEsc(_bD10(m.oppositionEndDate)) || "?"}` : "";
  const _arr   = v => (Array.isArray(v) ? v : (v ? [v] : []));   // OSIM dă viennaClasses ca text
  // TMview și buletinul dau aceleași coduri Viena în forme diferite — le unificăm, fără dubluri
  const vienna = [...new Set([..._arr(m.viennaCodes), ..._arr(m.viennaClasses)]
    .flatMap(v => String(v).split(/[;,]\s*/)).map(v => v.trim()).filter(Boolean))];
  const nature = [m.markFeature, m.kindMark].filter(Boolean).join(" · ");
  const officeUrl = /^https?:\/\//i.test(m.officeUrl || "")
    ? `<a href="${_bEsc(m.officeUrl)}" target="_blank" rel="noopener">Deschide dosarul</a>` : "";
  return `<div class="detail-grid">${[
    item("Status",                  _bEsc(m.markCurrentStatusCode || m.tradeMarkStatus)),
    item("Dată status",             _bEsc(_bD10(m.markCurrentStatusDate))),
    item("(111) Nr. înregistrare",  _bEsc(m.registrationNumber)),
    item("(151) Data înregistrare", _bEsc(_bD10(m.registrationDate))),
    item("(180) Data expirare",     _bEsc(_bD10(m.expiryDate))),
    item("Perioadă opoziție",       opp),
    item("(300) Prioritate revendicată", bulletinPrioritiesHtml(m)),
    item("Senioritate invocată",    bulletinSenioritiesHtml(m)),
    item("(550) Natura mărcii",     _bEsc(nature)),
    item("(531) Coduri Viena",      _bEsc(vienna.join(", "))),
    item("Culori revendicate",      _bEsc(Array.isArray(m.colorsClaimed) ? m.colorsClaimed.join(", ") : m.colorsClaimed)),
    item("Țări desemnate",          _bEsc((m.designatedCountries || []).join(", "))),
    item("Birou",                   _bEsc(m.tmOffice)),
    item("Dosar oficial",           officeUrl),
  ].join("")}</div>`;
}

// Toate datele unei mărci (folosit la comparație): identificare + solicitant/reprezentant
// + restul câmpurilor + clasele descrise.
function bulletinMarkDetailHtml(m) {
  const item = (label, val) =>
    `<div class="detail-item"><label style="display:block;">${label}</label><span>${val ? val : "—"}</span></div>`;
  const hasGoods = (m.goodAndServices || []).some(x => x.goodsAndServices);
  return `<div style="background:#f7f9fc;border-radius:8px;padding:12px 14px;">
    <div class="detail-grid">${[
      item("(210) Nr. cerere",     _bEsc(m.applicationNumber)),
      item("(220) Data depunere",  _bEsc(_bD10(m.applicationDate))),
      item("(442) Data publicare", _bEsc(_bD10(m.publicationDate))),
    ].join("")}</div>
    ${bulletinExtraFieldsHtml(m)}
    <div class="detail-grid" style="grid-template-columns:repeat(auto-fit,minmax(280px,1fr));">
      ${item("(731) Solicitant", bulletinApplicantsHtml(m))}
      ${item("(740) Reprezentant", bulletinRepsHtml(m))}
    </div>
    <div style="margin-top:6px;">
      <div style="font-size:.69rem;font-weight:700;color:#999;text-transform:uppercase;letter-spacing:.4px;margin-bottom:6px;">
        (511) Clase descrise${hasGoods ? "" : ' <span style="text-transform:none;font-weight:400;">— lista de produse/servicii încă indisponibilă în TMview</span>'}
      </div>
      ${bulletinClassesHtml(m)}
    </div>
  </div>`;
}

// (511) doar numerele claselor, pentru tabele — descrierea completă e pe pagina mărcii
function bulletinClassNumbers(m) {
  const nums = new Set();
  (m.niceClass || []).forEach(c => nums.add(String(c)));
  (m.niceDetailed || []).forEach(n => nums.add(String(n.class)));
  (m.goodAndServices || []).forEach(g => { if (g.niceClass) nums.add(String(g.niceClass)); });
  const arr = [...nums].filter(Boolean).sort((x, y) => Number(x) - Number(y));
  return arr.length ? arr.join(", ") : "—";
}

// Adresa paginii cu toate datele unei mărci (se deschide în tab nou)
function bulletinMarkUrl(source, date, appNumber) {
  return `/mark.html?source=${encodeURIComponent(source)}&date=${encodeURIComponent(date)}&app=${encodeURIComponent(appNumber)}`;
}
