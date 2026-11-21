"""Corectură de mână (21.11.2026) a mărcilor publicate de OSIM din cereri scanate cu recunoaștere
proastă de text (BOPI iunie 2018 și câteva rătăciri „~”/„?” din 2014–2024). Sursa: textul din buletin,
citit după sens („~i” → „și”, „rnedicale” → „medicale”); antetele de pagină lipite au fost scoase."""

# Clase rescrise integral.
FULL = {
    "M201804035": {
        "29": "Carne, pește, păsări și vânat, extracte din carne, fructe și legume conservate, congelate, uscate și fierte, jeleuri, dulcețuri, compoturi, ouă, lapte și produse lactate, uleiuri și grăsimi comestibile.",
        "30": "Cafea, ceai, cacao, înlocuitori de cafea, orez, tapioca și sago, făină și preparate din cereale, pâine, produse de patiserie și cofetărie, înghețată comestibilă, zahăr, miere, sirop de melasă, drojdie, praf de copt, sare, muștar, oțet, sosuri (condimente), mirodenii, gheață (apă înghețată).",
        "31": "Produse agricole, de acvacultură, horticole și forestiere, brute și neprelucrate, cereale și semințe crude și neprocesate, fructe și legume proaspete, plante aromatice proaspete, plante și flori naturale, bulbi, răsaduri și semințe pentru plantare, animale vii, hrană și băuturi pentru animale, malț.",
    },
    "M201804039": {
        "9": "Suporturi cu înregistrări audio, video, publicație electronică on-line descărcabilă.",
        "35": "Publicitate media on-air, on-line și pe alte suporturi pentru campanii de marketing, publicitate, curierat publicitar, distribuire de prospecte sau de eșantioane, organizare de expoziții cu scop publicitar, expunere de produse, promovare și ofertă de produse și/sau de servicii, activități de birou, de înregistrare, transcriere, compunere, compilare sau sistematizare a comunicărilor scrise sau a înregistrărilor, exploatarea sau compilarea datelor matematice sau statistice, ofertă de publicații și carte pe hârtie sau în format electronic, stabilirea de abonamente la publicații, servicii de abonament la publicații, relații publice.",
        "38": "Difuzare de programe audiovizuale, de radio sau de televiziune, în eter, prin satelit, prin rețele de comunicații sau prin alte mijloace, transmisii de date, servicii de telecomunicații.",
        "41": "Organizare de programe și de evenimente instructiv-educative și de divertisment, realizare de programe artistice și de emisiuni audiovizuale, filmări și înregistrări video și/sau audio, montaje de înregistrări video și audio, producție de film, închiriere de filme, de înregistrări audio sau video, instruire, activități culturale, de divertisment, sportive, organizare de concursuri educative sau de divertisment, publicare on-line.",
        "42": "Crearea și înregistrarea de site-uri web, găzduire de site-uri web, închiriere de servere web.",
        "45": "Servicii personale și sociale oferite pentru satisfacerea unor nevoi individuale, cluburi.",
    },
    "M201804060": {
        "5": "Preparate farmaceutice, medicale și veterinare, preparate sanitare pentru scopuri medicale, substanțe și alimente dietetice adaptate pentru scopuri medicale sau veterinare, alimente pentru sugari, suplimente dietetice de uz uman și veterinar, plasturi, materiale de pansat, materiale pentru plombarea dinților și pentru amprente dentare, dezinfectanți, produse pentru distrugerea dăunătorilor, fungicide, erbicide. (Solicităm protecție pentru întreaga listă de produse incluse în această clasă conform Clasificării de la Nisa)",
        "10": "Aparate și instrumente chirurgicale, medicale, dentare și veterinare, membre, ochi și dinți artificiali, articole ortopedice, materiale de sutură, dispozitive terapeutice și de asistență adaptate pentru persoanele cu handicap, aparate de masaj, aparate, dispozitive și articole pentru sugari, aparate, dispozitive și articole pentru activitate sexuală. (Solicităm protecție pentru întreaga listă de produse incluse în această clasă conform Clasificării de la Nisa)",
        "35": "Publicitate, managementul afacerilor, administrarea afacerilor, funcții de birou. (Solicităm protecție pentru întreaga listă de servicii incluse în această clasă conform Clasificării de la Nisa)",
    },
    "M201804063": {
        "44": "Servicii medicale, servicii veterinare, servicii de îngrijire sanitară și de înfrumusețare pentru persoane și animale, servicii de agricultură, horticultură și silvicultură.",
    },
    "M201804126": {
        "40": "Servicii de aplicare de folii pe geamurile și caroseriile autovehiculelor.",
    },
    "M201804148": {
        "6": "Metale comune și aliaje ale acestora, materiale metalice pentru structuri și construcții, construcții metalice transportabile, cabluri și fire neelectrice din metal comun, fierărie, articole mici din armătură metalică, containere din metal pentru depozitare sau transport, seifuri.",
        "35": "Publicitate, managementul afacerilor, administrarea afacerilor, funcții de birou.",
    },
    "M201804149": {
        "6": "Metale comune și aliaje ale acestora, materiale metalice pentru structuri și construcții, construcții metalice transportabile, cabluri și fire neelectrice din metal comun, fierărie, articole mici din armătură metalică, containere din metal pentru depozitare sau transport, seifuri.",
        "35": "Publicitate, managementul afacerilor, administrarea afacerilor, funcții de birou.",
        "37": "Construcții de clădiri, reparații, servicii de instalații.",
        "42": "Servicii științifice și tehnologice, precum și de cercetare și proiectare în legătură cu acestea, analize industriale și servicii de cercetare, proiectarea și dezvoltarea de componente hardware și software ale calculatorului.",
        "43": "Servicii de furnizare de alimente și băuturi, cazare temporară.",
    },
    "M201804153": {
        "9": "Contoare de parcare, cronografe (aparate de înregistrare a timpului), comutator temporizat automat, mecanisme pentru aparatele acționate cu fise, aparate de înregistrare a distanței, aparate pentru înregistrarea distanței, panouri informative cu afișaj electronic, cititoare de cărți electronice, cititoare (echipament de procesare a datelor), tag-uri de proximitate electronice ca aparate de control de la distanță, carduri magnetice codate, cartele codate, aparate de măsurare, detectoare cu infraroșu, circuite integrate, carduri cu circuite integrate (carduri inteligente), dispozitive de intercomunicare, terminale interactive cu ecran tactil, aparate de marcat fiscal, aparate de monitorizare, altele decât cele pentru scopuri medicale, programe de calculator înregistrate, programe de calculator (software descărcabil), componente software pentru calculator înregistrate, aparate de înregistrare video, aplicații software pentru calculator, descărcabile, platforme software de calculator, înregistrate sau descărcabile, aplicații software pentru parcarea automată a vehiculelor, aplicații mobile descărcabile pentru gestionarea de date, aplicații mobile descărcabile pentru transmiterea de date, aplicații mobile descărcabile pentru transmiterea de informații, aplicații mobile descărcabile pentru gestionarea de informații, aplicații software pentru comanda parcării automate a mașinilor, aplicații software pentru plăți electronice, aparate de procesare a datelor, aparate pentru măsurarea distanței, roboți telefonici, receptoare audio-video, stații de încărcare pentru vehicule electrice, sisteme de poziționare globală (GPS), modem-uri, aparate de navigație prin satelit, receptoare audio-video.",
        "35": "Administrarea programelor de fidelizare a consumatorilor, asistență în managementul afacerilor, consultanță în organizarea și managementul afacerilor, consultanță în managementul afacerilor, servicii de consiliere privind managementul afacerilor, consultanță profesională în afaceri, managementul afacerilor cu privire la furnizorii de servicii independenți, servicii de intermediere a afacerilor cu privire la potrivirea potențialilor investitori privați cu antreprenorii care au nevoie de finanțare, administrare comercială privind licențierea bunurilor și serviciilor aparținând terților, compilarea de informații în baze de date computerizate, managementul computerizat al fișierelor, marketing, administrarea afacerilor pentru parcări auto, facturare, publicitate online pe o rețea de calculatoare, furnizarea unei piețe comerciale online pentru cumpărătorii și vânzătorii de bunuri și servicii.",
        "36": "Schimb de bani, evaluare financiară (asigurări, servicii bancare, imobiliare), management financiar, analiză financiară, consultanță financiară, managementul financiar al plăților/rambursărilor pentru terți, brokeraj de asigurări, investiții în fonduri, procesarea plăților efectuate cu cardul de credit, procesarea plăților efectuate cu cardul de debit, închirierea bunurilor imobiliare, servicii de agenție imobiliară, consultanță în asigurări, management financiar, emiterea jetoanelor valorice.",
        "39": "Servicii de folosire în comun a mașinii, furnizarea de direcții de deplasare în scopuri de călătorie, închirierea sistemelor de navigație, închirierea locurilor de parcare, parcarea de mașini, închirierea de mașini, transportul mașinilor, transportul de pasageri, închirierea de garaje, remorcare, tractare, transport, informații legate de transporturi, logistică de transport, rezervări de transport, rezervarea locurilor de călătorie, furnizare de informații despre servicii de parcare a vehiculelor.",
        "42": "Crearea și mentenanța site-urilor web pentru terți, crearea și proiectarea pentru terți de indexuri de informații bazate pe site-urile web (servicii de tehnologia informației), găzduirea site-urilor (site-uri web), furnizarea de informații despre tehnologia calculatoarelor și programare prin intermediul unui site web, software ca serviciu, dezvoltarea de software în cadrul publicării de software, consultanță în proiectarea site-urilor web, platformă ca serviciu, furnizori de servicii de externalizare în domeniul tehnologiei informației, monitorizarea sistemelor de calcul (calculatoarelor) prin acces de la distanță, închirierea de software pentru calculatoare.",
    },
    "M201804169": {
        "18": "Piele și imitații de piele, piei de animale și piei brute, bagaje și genți de transport, umbrele de ploaie și de soare, bastoane, bice, harnașament și articole de șelărie, zgărzi, lese și îmbrăcăminte pentru animale.",
    },
    "M201804178": {
        "18": "Piele și imitații de piele, umbrele și parasolare, genți.",
        "25": "Curele (accesorii vestimentare), articole vestimentare pentru bărbați, îmbrăcăminte, încălțăminte pentru sport.",
    },
    "M201804055": {
        "2": "Coloranți, lacuri și vopsele, produse pentru prevenirea coroziunii și a deteriorării lemnului, materiale pentru vopsit, mordanți, rășini naturale în stare brută.",
        "9": "Aparate și instrumente științifice.",
        "10": "Aparate și instrumente chirurgicale, medicale, dentare și veterinare.",
        "11": "Aparate de iluminat, de încălzit, de producere a vaporilor, de coacere, de refrigerare, de uscare, de ventilare, de distribuire a apei și instalații sanitare.",
        "42": "Servicii științifice și tehnologice, precum și servicii de cercetare și de creație, referitoare la acestea, servicii de analiză și cercetare industrială.",
        "44": "Servicii medicale, servicii veterinare, servicii de igienă și de îngrijire a frumuseții pentru oameni sau animale, servicii din agricultură, horticultură și silvicultură.",
    },
    "M201804053": {
        "41": "Educație, instruire, divertisment, activități sportive și culturale.",
        "42": "Servicii științifice și tehnologice, precum și servicii de cercetare și de creație, referitoare la acestea, servicii de analiză și cercetare industrială, crearea și dezvoltarea calculatoarelor și a programelor de calculator.",
    },
}

_EDU = "Educație, instruire, divertisment, activități sportive și culturale."
_ORG = ("Educație, instruire, divertisment, activități sportive și culturale, inclusiv, dar fără limitare la: "
        "organizarea și susținerea colocviilor, concertelor, conferințelor, congreselor, seminarelor, spectacolelor, "
        "prezentări cinematografice, organizarea de competiții (educație sau amuzament), prezentări în săli "
        "cinematografice, publicarea cărților și jurnalelor electronice online, publicarea textelor, altele decât "
        "textele publicitare.")
_C35 = "Publicitate, conducerea și administrarea afacerilor, lucrări de birou."
_C36 = "Asigurări, afaceri financiare, monetare, imobiliare."
_C43 = "Servicii de alimentație publică, servicii de cazare temporară."
for _a in ("M201804040", "M201804041", "M201804042", "M201804045", "M201804046"):
    FULL[_a] = {"35": _C35, "36": _C36, "41": _ORG, "43": _C43}
FULL["M201804051"] = {"38": "Telecomunicații.", "41": _EDU}
FULL["M201804052"] = {"41": _EDU}
FULL["M201804140"] = {"35": "Publicitate.", "38": "Telecomunicații.", "41": _EDU}
FULL["M201804164"] = {"35": "Publicitate.", "41": "Servicii de divertisment, educație, divertisment și activități sportive.",
                      "43": "Asigurare de hrană și băuturi, cazare temporară."}
FULL["M201804165"] = {"35": "Publicitate.", "41": "Servicii de divertisment, educație, divertisment și activități sportive.",
                      "43": "Asigurare de hrană și băuturi, cazare temporară."}

# Corecturi punctuale (restul listei e bun).
SPOT = {
    "M201403848": {"h?rtie": "hârtie"},
    "M201407435": {"imitat?de piele": "imitații de piele"},
    "M201500823": {"lum?nari": "lumânări"},
    "M201701516": {"v?nat": "vânat", "extracte de came": "extracte de carne"},
    "M202103342": {"înaltă~performanță": "înaltă performanță"},
    "M202306061": {"în~scopuri": "în scopuri"},
    "M202311446": {"adezivep~entru~genţi": "adezive pentru genţi", "învelifon~pentru s'chiuri": "învelitori pentru schiuri"},
    "M202400132": {"adezive-p~entru~genţi": "adezive pentru genţi", "învelifon~pentru s'chiuri": "învelitori pentru schiuri"},
    "M202311762": {"formă~debomboane": "formă de bomboane"},
    "M202311811": {"cinematografelor~săli": "cinematografelor, săli"},
    "M202407076": {"cinematografelor~săli": "cinematografelor, săli"},
}

# A doua tură (21.11.2026), găsite de verificarea ocr_scan.
FULL["M201804157"] = {"43": _C43}
FULL["M201804043"] = {"35": _C35, "36": _C36, "41": _ORG.replace("la: ", "la "), "43": _C43}
FULL["M201804044"] = {"35": _C35, "36": _C36, "41": _ORG, "43": _C43}
FULL["M201804070"] = {
    "16": "Produse de imprimerie și tipografie, fotografii, publicații (ziare și reviste cu sau fără caracter de periodice), articole de birou, materiale publicitare (tipărituri).",
    "35": "Publicitate, gestiunea afacerilor comerciale, administrație comercială, lucrări de birou, compilarea de informații în baze de date computerizate, organizare de târguri și expoziții în scop comercial, publicitar și de promovare.",
    "38": "Telecomunicații, difuzarea de programe radio și/sau TV (programe și generice radio și/sau TV), consultanță în domeniul mass-media, furnizarea de camere de discuții în spațiu virtual, respectiv pe internet (internet chatrooms).",
    "41": "Educație, instruire, divertisment, activități sportive și culturale, filmare, organizarea de târguri și expoziții în scop educativ.",
    "42": "Crearea și dezvoltarea programelor de calculator, crearea, întreținerea și găzduirea de pagini și site-uri web, furnizarea de motoare de căutare pe internet.",
}
FULL["M201804077"] = {"9": "Aplicații software (programe)."}
FULL["M201804078"] = {
    "9": "Suporturi magnetice pentru înregistrări audio, casete audio digitale, CD-uri audio, discuri audio digitale, discuri pentru (înregistrări audio).",
    "38": "Difuzare audio.",
    "41": "Prezentări audiovizuale, producție de înregistrări audio, servicii de divertisment audio, producție de înregistrări audiovizuale.",
}
_BERE = ("Bere, ape minerale și gazoase și alte băuturi nealcoolice, băuturi din fructe și sucuri de fructe, siropuri și "
         "alte preparate pentru fabricarea băuturilor. (Solicităm protecție pentru întreaga listă de produse incluse în "
         "această clasă conform Clasificării de la Nisa)")
FULL["M201804019"] = {"32": _BERE}
FULL["M201804021"] = {"32": _BERE}
SPOT["M202106955"] = {"cornunicarea": "comunicarea"}
SPOT["M201903184"] = {"cornunicaţii": "comunicaţii"}
SPOT["M201902569"] = {"cornunicaţii": "comunicaţii"}
SPOT["M201808704"] = {"rnedicală": "medicală"}


def apply(app_no: str, goods: dict) -> dict:
    g = dict(goods or {})
    for c, t in FULL.get(app_no, {}).items():
        g[c] = t
    for b, ok in SPOT.get(app_no, {}).items():
        g = {c: t.replace(b, ok) for c, t in g.items()}
    return g
