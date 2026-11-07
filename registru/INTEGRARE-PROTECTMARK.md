# Integrarea ProWatch în protectmark.ro (WordPress + Elementor)

Se face la lansare. Ambele bucăți de cod se pun în câte un widget **HTML** din Elementor.

## 1. Pagina „Verificare marcă” (ProWatch încorporat)

```html
<iframe id="prowatch" src="https://prowatch.protectmark.ro/?embed=1" title="Verificare mărci OSIM — ProWatch"
        style="width:100%;border:0;min-height:900px;display:block" loading="lazy"></iframe>
<script>
  // Înălțimea iframe-ului urmează conținutul ProWatch (fără bară de derulare dublă).
  window.addEventListener("message", function (e) {
    if (e.origin === "https://prowatch.protectmark.ro" && e.data && e.data.prowatchHeight)
      document.getElementById("prowatch").style.height = (e.data.prowatchHeight + 20) + "px";
  });
  // Căutarea pornită din căsuța de pe prima pagină: ?name=... → se transmite iframe-ului.
  (function () {
    var q = location.search.replace(/^\?/, "");
    if (q) document.getElementById("prowatch").src = "https://prowatch.protectmark.ro/cauta.html?" + q + "&embed=1";
  })();
</script>
```

## 2. Căsuța de căutare (prima pagină / pagini de servicii)

Înlocuiți `/verificare-marca/` cu adresa paginii de la punctul 1.

```html
<form action="/verificare-marca/" method="get" style="display:flex;gap:8px;max-width:640px;margin:auto">
  <input name="name" required placeholder="Verifică o denumire de marcă (ex. MUSCLE SAUCE)"
         style="flex:1;padding:12px 14px;border:1.5px solid #e6e4e1;border-radius:8px;font-size:16px">
  <button style="background:#e21f25;color:#fff;border:0;border-radius:8px;padding:12px 20px;font-weight:700;cursor:pointer">Caută</button>
</form>
```

## 3. La lansare

- Railway → serviciul prowatch → Variables: `INDEXING=1` (permite indexarea în Google).
- Opțional: buton în meniul WordPress către pagina „Verificare marcă”.
