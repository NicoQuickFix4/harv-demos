/* Multi-step quote widget — replaces the simple quote-form in the hero.
   Step 0: Contact (name, phone, email, postcode, address, dienst multi-select)
   Step 1: Wat kunnen we voor u doen?
   Step 2: Wat voor dak heeft u?
   Step 3: Welk materiaal? (conditional)
   Step 4: Hoe groot is het dak?
   Step 5: Wanneer wilt u het laten uitvoeren?
   Step 6: Foto upload (optional)
   Step 7: Confirmation
*/

const { useState, useMemo, useRef, useEffect } = React;

// Concept-opslag: bewaart de ingevulde antwoorden in de sessie, zodat een
// onderbroken bezoeker (vooral mobiel) verdergaat waar 'ie was i.p.v. opnieuw
// te beginnen. De foto (File) is niet serialiseerbaar en wordt overgeslagen.
// Aparte sleutel per volgorde, zodat een concept van de ene template niet op de
// verkeerde stapindex landt in de andere.
function draftKey(engageFirst, combinedContact) {
  return 'qw-draft-v2-' + (combinedContact ? 'cc' : (engageFirst ? 'ef' : 'cf'));
}
function loadDraft(engageFirst, combinedContact) {
  try {
    const raw = sessionStorage.getItem(draftKey(engageFirst, combinedContact));
    return raw ? JSON.parse(raw) : null;
  } catch (_) { return null; }
}

// ───── SVG icon registry ─────
const Icon = {
  house: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 11 12 3l9 8v10H3z"/><path d="M9 21V13h6v8"/>
    </svg>
  ),
  wrench: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M14.7 6.3a4.5 4.5 0 0 1 5 5.7l-9.7 9.7-4.2-4.2 9.7-9.7a4.5 4.5 0 0 1-.8-1.5z"/><path d="M9.5 9.5 4 4"/>
    </svg>
  ),
  drop: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 3s7 8 7 13a7 7 0 0 1-14 0c0-5 7-13 7-13z"/>
    </svg>
  ),
  shield: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 2 4 6v6c0 5 3.5 9 8 10 4.5-1 8-5 8-10V6l-8-4Z"/><path d="m9 12 2 2 4-4"/>
    </svg>
  ),
  gutter: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 10h18v3H3z"/><path d="M5 13v8M19 13v8M7 17h2M15 17h2"/>
    </svg>
  ),
  window: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <rect x="4" y="3" width="16" height="18" rx="1"/><path d="M12 3v18M4 12h16"/>
    </svg>
  ),
  search: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>
    </svg>
  ),
  question: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 1 1 3.5 2.3c-.7.4-1 .9-1 1.7M12 17h.01"/>
    </svg>
  ),
  flat: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 12h20M4 12v9h16v-9M6 12V9h12v3"/>
    </svg>
  ),
  slope: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="m2 14 10-9 10 9M5 12v9h14v-9"/>
    </svg>
  ),
  both: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="m2 12 6-6 6 6M8 12v8h12v-8M2 12v8h6"/>
    </svg>
  ),
  bolt: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="m13 2-9 12h7l-1 8 9-12h-7z"/>
    </svg>
  ),
  calendar: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="5" width="18" height="16" rx="2"/><path d="M8 3v4M16 3v4M3 10h18"/>
    </svg>
  ),
  clock: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>
    </svg>
  ),
  upload: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12"/>
    </svg>
  ),
  check: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M20 6 9 17l-5-5"/>
    </svg>
  ),
  arrow: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M5 12h14M13 5l7 7-7 7"/>
    </svg>
  ),
  arrowLeft: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M19 12H5M11 5l-7 7 7 7"/>
    </svg>
  ),
};

// ───── Options data ─────
const SERVICES = [
  { id: 'nieuw_dak', label: 'Nieuw dak', icon: Icon.house },
  { id: 'renovatie', label: 'Dakrenovatie', icon: Icon.wrench },
  { id: 'reparatie', label: 'Reparatie / lekkage', icon: Icon.drop },
  { id: 'isolatie', label: 'Dakisolatie', icon: Icon.shield },
  { id: 'dakgoot', label: 'Dakgoot / zinkwerk', icon: Icon.gutter },
  { id: 'dakraam', label: 'Dakraam', icon: Icon.window },
  { id: 'inspectie', label: 'Inspectie / advies', icon: Icon.search },
];

const DAKTYPES = [
  { id: 'plat', label: 'Plat dak', icon: Icon.flat },
  { id: 'schuin', label: 'Schuin dak', icon: Icon.slope },
  { id: 'allebei', label: 'Allebei', icon: Icon.both },
  { id: 'weet_niet', label: 'Weet ik niet', icon: Icon.question },
];

const MATERIALEN_SCHUIN = [
  { id: 'dakpannen', label: 'Dakpannen' },
  { id: 'leien', label: 'Leien' },
  { id: 'riet', label: 'Riet' },
  { id: 'weet_niet', label: 'Weet ik niet' },
];

const MATERIALEN_PLAT = [
  { id: 'bitumen', label: 'Bitumen' },
  { id: 'epdm', label: 'EPDM' },
  { id: 'pvc', label: 'PVC' },
  { id: 'zink', label: 'Zink' },
  { id: 'weet_niet', label: 'Weet ik niet' },
];

const GROOTTE = [
  { id: 'tot_50', label: 'Tot 50 m²', sub: 'Klein dak' },
  { id: '50_100', label: '50 – 100 m²', sub: 'Gemiddeld' },
  { id: 'over_100', label: 'Meer dan 100 m²', sub: 'Groot dak' },
  { id: 'weet_niet', label: 'Weet ik niet', sub: 'Wij meten gratis op' },
];

const TIMING = [
  { id: 'spoed', label: 'Spoed', sub: 'Lekkage of schade', icon: Icon.bolt },
  { id: 'maand', label: 'Binnen een maand', sub: 'Snelle planning', icon: Icon.calendar },
  { id: '1_3_maanden', label: '1 – 3 maanden', sub: 'Comfortabele termijn', icon: Icon.clock },
  { id: 'orienterend', label: 'Alleen oriënterend', sub: 'Vrijblijvend kijken', icon: Icon.search },
];

// ───── Steps definition ─────
// Twee volgordes; per template gekozen via window.__qwEngageFirst.
//  - contact-first (standaard, o.a. helder): contactgegevens eerst.
//  - engage-first (origineel zet de vlag): makkelijke dienstvraag eerst,
//    contactgegevens als laatste stap.
// E-mail is verplicht op de contactstap, dus elke verstuurde lead heeft een
// echt e-mailadres — in beide volgordes.
const DAK_STEPS = [
  { id: 'daktype', title: 'Wat voor dak heeft u?', sub: '' },
  { id: 'materiaal', title: 'Welk materiaal?', sub: 'Optioneel: kies "weet ik niet" als u twijfelt' },
  { id: 'grootte', title: 'Hoe groot is het dak?', sub: 'Een schatting is genoeg' },
  { id: 'timing', title: 'Wanneer wilt u het laten uitvoeren?', sub: '' },
  { id: 'foto', title: 'Foto van het dak', sub: 'Optioneel, versnelt de offerte' },
];
const DIENST_STEP = { id: 'dienst', title: 'Wat kunnen we voor u doen?', sub: 'Kies waar we u mee kunnen helpen (meerdere mag)' };
const DONE_STEP = { id: 'done', title: 'Bedankt!', sub: '' };
const STEPS_CONTACT_FIRST = [
  { id: 'contact', title: 'Vraag offerte aan', sub: 'Eerst even uw contactgegevens' },
  DIENST_STEP, ...DAK_STEPS, DONE_STEP,
];
const STEPS_ENGAGE_FIRST = [
  DIENST_STEP, ...DAK_STEPS,
  { id: 'contact', title: 'Waar mogen we de offerte naartoe sturen?', sub: 'Uw gegevens, u zit nergens aan vast' },
  DONE_STEP,
];
// Gecombineerde variant: contactgegevens én de dienstvraag samen op de eerste
// stap (meer in één scherm). Geen losse dienst-stap.
const STEPS_CONTACT_COMBINED = [
  { id: 'contact', title: 'Vraag offerte aan', sub: 'Uw gegevens en waar we u mee kunnen helpen' },
  ...DAK_STEPS, DONE_STEP,
];
function isEngageFirst() {
  return typeof window !== 'undefined' && !!window.__qwEngageFirst;
}
function getSteps(engageFirst, combinedContact) {
  if (combinedContact) return STEPS_CONTACT_COMBINED;
  return engageFirst ? STEPS_ENGAGE_FIRST : STEPS_CONTACT_FIRST;
}

function getCompanyName() {
  const d = window.__tplData || {};
  return (d.COMPANY_NAME || 'ons').trim();
}

// ───── Validation (Nederlandse telefoon / postcode / e-mail) ─────
const RE_EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function normalizePhone(v) {
  return String(v || '').replace(/[\s\-().]/g, '').replace(/^0031/, '+31');
}
function isValidPhone(v) {
  const n = normalizePhone(v);
  // +31 gevolgd door 9 cijfers (zonder leidende 0), of lokaal 0xxxxxxxxx.
  return /^\+31[1-9]\d{8}$/.test(n) || /^0[1-9]\d{8}$/.test(n);
}
function formatPhone(v) {
  let n = normalizePhone(v);
  if (/^0[1-9]\d{8}$/.test(n)) n = '+31' + n.slice(1);
  const m = n.match(/^\+31([1-9]\d{8})$/);
  return m ? '+31 ' + m[1] : v;
}
function isValidPostcode(v) {
  return /^[1-9][0-9]{3}\s?[A-Za-z]{2}$/.test(String(v || '').trim());
}
function formatPostcode(v) {
  const n = String(v || '').replace(/\s/g, '').toUpperCase();
  const m = n.match(/^([1-9][0-9]{3})([A-Z]{2})$/);
  return m ? m[1] + ' ' + m[2] : v;
}
function isValidName(v) { return String(v || '').trim().length >= 2; }
function isValidEmail(v) { return RE_EMAIL.test(String(v || '').trim()); }

const CONTACT_FIELDS = ['naam', 'telefoon', 'email', 'postcode'];
function validateContact(d) {
  const e = {};
  if (!isValidName(d.naam)) e.naam = 'Vul uw volledige naam in.';
  if (!isValidPhone(d.telefoon)) e.telefoon = 'Geldig Nederlands nummer, bijv. +31 6 12345678.';
  if (!isValidEmail(d.email)) e.email = 'Vul een geldig e-mailadres in.';
  if (!isValidPostcode(d.postcode)) e.postcode = 'Geldige postcode, bijv. 1234 AB.';
  return e;
}

// ───── Lead-payload + verzending ─────
function labelOf(arr, id) { const o = arr.find(x => x.id === id); return o ? o.label : id; }
function buildLeadPayload(d) {
  return {
    naam: d.naam.trim(),
    telefoon: formatPhone(d.telefoon),
    email: d.email.trim(),
    postcode: formatPostcode(d.postcode),
    adres: (d.adres || '').trim(),
    diensten: d.diensten.map(id => labelOf(SERVICES, id)),
    daktype: d.daktype ? labelOf(DAKTYPES, d.daktype) : null,
    materiaal: d.materiaal || null,
    grootte: d.grootte ? labelOf(GROOTTE, d.grootte) : null,
    timing: d.timing ? labelOf(TIMING, d.timing) : null,
    foto: d.foto ? d.foto.name : null,
    bron: (window.__tplData || {}).COMPANY_NAME || null,
    ingestuurd_op: new Date().toISOString(),
  };
}
// Verstuurt de lead naar window.__tplData.LEAD_ENDPOINT als die is ingesteld.
// Zonder endpoint draait de widget in demo-modus: hij logt de lead en slaagt,
// zodat het bedankscherm verschijnt. Een ingesteld endpoint krijgt echte POSTs.
async function sendLead(payload) {
  const endpoint = (window.__tplData || {}).LEAD_ENDPOINT;
  if (!endpoint) {
    await new Promise(r => setTimeout(r, 450));
    try { console.info('[quote-widget] lead (demo, geen endpoint):', payload); } catch (_) {}
    return;
  }
  const res = await fetch(endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error('HTTP ' + res.status);
}

// ───── Self-contained validatie-styles (widget is gedeeld door beide templates) ─────
const QW_VALIDATION_STYLE_ID = 'qw-validation-style';
function ensureValidationStyles() {
  if (typeof document === 'undefined' || document.getElementById(QW_VALIDATION_STYLE_ID)) return;
  const el = document.createElement('style');
  el.id = QW_VALIDATION_STYLE_ID;
  el.textContent = `
    .qw-field { display: flex; flex-direction: column; gap: 5px; min-width: 0; }
    .qw-row-2 { align-items: start; }
    .qw-input.is-invalid, .qw-input.is-invalid:focus { border-color: #e5484d; }
    .qw-error { font-size: 12.5px; line-height: 1.35; color: #c4221f; font-weight: 600; }
    .qw-submit-error {
      display: flex; align-items: flex-start; gap: 8px;
      font-size: 13.5px; line-height: 1.4; color: #c4221f; font-weight: 600;
      background: #fdecec; border: 1px solid #f7c4c4; border-radius: 10px;
      padding: 10px 12px; margin-top: 14px;
    }
    .qw-cta[aria-busy="true"] { opacity: .82; cursor: progress; }
    .qw-reassure {
      display: flex; align-items: center; justify-content: center; gap: 7px;
      margin: 13px 0 0; font-size: 12.5px; font-weight: 500;
      color: #5a6473; text-align: center;
    }
    .qw-reassure-ic { display: inline-flex; width: 15px; height: 15px; color: var(--c-accent, #f06a2a); }
    .qw-reassure-ic svg { width: 15px; height: 15px; }
  `;
  document.head.appendChild(el);
}

function QuoteWidget({ engageFirst = isEngageFirst(), combinedContact = false } = {}) {
  ensureValidationStyles();
  const STEPS = getSteps(engageFirst, combinedContact); // volgorde/variant per instance
  const draft = loadDraft(engageFirst, combinedContact);
  const DEFAULT_D = {
    naam: '', telefoon: '+31 ', email: '', postcode: '', adres: '',
    diensten: [],
    service: null,
    daktype: null,
    materiaal: null,
    grootte: null,
    timing: null,
    foto: null,
  };
  // Herstel nooit naar de laatste (done-)stap; max. de contactstap.
  const [step, setStep] = useState(draft ? Math.min(Math.max(0, draft.step | 0), STEPS.length - 2) : 0);
  const [d, setD] = useState(() => ({ ...DEFAULT_D, ...(draft && draft.d ? draft.d : {}), foto: null }));
  const [touched, setTouched] = useState({});
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState(null);
  const submittedRef = useRef(false);
  const submittingRef = useRef(false); // synchrone guard tegen dubbel-klik (state is async)

  const contactErrors = useMemo(() => validateContact(d), [d.naam, d.telefoon, d.email, d.postcode]);

  // Compute which steps are active (materiaal skipped if daktype = weet_niet)
  const stepFlow = useMemo(() => {
    const skipMateriaal = d.daktype === 'weet_niet';
    return STEPS.filter(s => !(skipMateriaal && s.id === 'materiaal'));
  }, [d.daktype]);

  const current = stepFlow[step];
  // 'done' is zelf de laatste stap in stepFlow; de laatste *actie*-stap (waar we
  // versturen) is dus de stap dáárvoor.
  const isFinalStep = step === stepFlow.length - 2;
  const progress = (step / (stepFlow.length - 1)) * 100;

  // Bewaar concept (zonder foto) bij elke wijziging; niet op het bedankscherm.
  useEffect(() => {
    if (submittedRef.current || current.id === 'done') return;
    try {
      const { foto, ...rest } = d;
      sessionStorage.setItem(draftKey(engageFirst, combinedContact), JSON.stringify({ step, d: rest }));
    } catch (_) {}
  }, [d, step]);

  const set = (k, v) => setD(prev => ({ ...prev, [k]: v }));
  const toggleDienst = (id) => setD(prev => ({
    ...prev,
    diensten: prev.diensten.includes(id)
      ? prev.diensten.filter(x => x !== id)
      : [...prev.diensten, id]
  }));

  const next = () => setStep(s => Math.min(s + 1, stepFlow.length - 1));
  const back = () => { setSubmitError(null); setStep(s => Math.max(s - 1, 0)); };

  // Normaliseer + valideer telefoon/postcode bij verlaten van het veld.
  const onBlurField = (k) => {
    setD(prev => {
      if (k === 'telefoon') return { ...prev, telefoon: formatPhone(prev.telefoon) };
      if (k === 'postcode') return { ...prev, postcode: formatPostcode(prev.postcode) };
      return prev;
    });
    setTouched(t => ({ ...t, [k]: true }));
  };

  // Verstuurt de lead; voorkomt dubbel verzenden en toont fouten.
  const submit = async () => {
    if (submittingRef.current || submittedRef.current) return;
    submittingRef.current = true;
    setSubmitError(null);
    setSubmitting(true);
    try {
      await sendLead(buildLeadPayload(d));
      submittedRef.current = true;
      try { sessionStorage.removeItem(draftKey(engageFirst, combinedContact)); } catch (_) {}
      setStep(stepFlow.length - 1);
    } catch (err) {
      setSubmitError('Versturen is niet gelukt. Controleer uw verbinding en probeer het opnieuw.');
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  };

  // Selectiestappen: knop pas actief bij een keuze.
  const selectionReady = () => {
    if (current.id === 'dienst') return d.diensten.length > 0;
    if (current.id === 'daktype') return !!d.daktype;
    if (current.id === 'materiaal') return !!d.materiaal;
    if (current.id === 'grootte') return !!d.grootte;
    if (current.id === 'timing') return !!d.timing;
    return true;
  };
  // Contact- en fotostap houden de knop actief zodat we bij klikken duidelijke
  // validatie-feedback kunnen tonen i.p.v. een 'dode' uitgeschakelde knop.
  const navDisabled = (current.id === 'contact' || current.id === 'foto')
    ? submitting
    : !selectionReady();

  const handleNext = () => {
    if (current.id === 'contact') {
      setTouched({ naam: true, telefoon: true, email: true, postcode: true });
      const cleaned = { ...d, telefoon: formatPhone(d.telefoon), postcode: formatPostcode(d.postcode) };
      setD(cleaned);
      const errs = validateContact(cleaned);
      if (Object.keys(errs).length) {
        const first = CONTACT_FIELDS.find(k => errs[k]);
        requestAnimationFrame(() => {
          const el = document.getElementById('qw-' + first);
          if (el) el.focus();
        });
        return;
      }
      // Na geldige gegevens: versturen als contact de laatste stap is
      // (engage-first), anders door naar de volgende stap (contact-first).
      if (isFinalStep) { submit(); return; }
      next();
      return;
    }
    if (isFinalStep) { submit(); return; }
    next();
  };

  return (
    <div className="qw">
      <div className="qw-tagline">
        <span className="qw-tagline-dot"></span>
        Hé eigenaar, test mij <span style={{opacity:.7}}>;)</span>
      </div>

      <div className="qw-card">
        <div className="qw-progress">
          <div className="qw-progress-bar" style={{ width: progress + '%' }}></div>
        </div>

        <div className="qw-head">
          <h3>{current.title}</h3>
          {current.sub && <p>{current.sub}</p>}
        </div>

        <div className="qw-body" key={current.id}>
          {current.id === 'dienst' && (
            <DienstStep diensten={d.diensten} toggleDienst={toggleDienst} />
          )}
          {current.id === 'contact' && (
            <ContactStep d={d} set={set} errors={contactErrors} touched={touched} onBlurField={onBlurField}
              showDienst={combinedContact} toggleDienst={toggleDienst} />
          )}
          {current.id === 'daktype' && (
            <OptionGrid columns={2} options={DAKTYPES} value={d.daktype} onChange={v => set('daktype', v)} />
          )}
          {current.id === 'materiaal' && (
            <OptionGrid
              columns={2}
              options={d.daktype === 'plat' ? MATERIALEN_PLAT : MATERIALEN_SCHUIN}
              value={d.materiaal}
              onChange={v => set('materiaal', v)}
            />
          )}
          {current.id === 'grootte' && (
            <OptionList options={GROOTTE} value={d.grootte} onChange={v => set('grootte', v)} />
          )}
          {current.id === 'timing' && (
            <OptionList options={TIMING} value={d.timing} onChange={v => set('timing', v)} />
          )}
          {current.id === 'foto' && (
            <PhotoStep value={d.foto} onChange={v => set('foto', v)} onSkip={handleNext} submitting={submitting} />
          )}
          {current.id === 'done' && (
            <Confirmation d={d} />
          )}
        </div>

        {current.id !== 'done' && (
          <>
            {submitError && (
              <div className="qw-submit-error" role="alert">
                <span className="qw-cta-ic" aria-hidden="true">{Icon.bolt}</span>
                {submitError}
              </div>
            )}
            <div className="qw-nav">
              {step > 0 ? (
                <button type="button" className="qw-back" onClick={back} disabled={submitting}>
                  <span className="qw-back-ic">{Icon.arrowLeft}</span>
                  Terug
                </button>
              ) : <span/>}
              <button
                type="button"
                className="qw-cta"
                disabled={navDisabled}
                aria-busy={submitting ? 'true' : undefined}
                onClick={handleNext}>
                <span>{submitting ? 'Versturen…' : (isFinalStep ? 'Versturen' : 'Volgende')}</span>
                {!submitting && <span className="qw-cta-ic">{Icon.arrow}</span>}
              </button>
            </div>
            <p className="qw-reassure">
              <span className="qw-reassure-ic" aria-hidden="true">{Icon.check}</span>
              Gratis &amp; vrijblijvend &middot; reactie binnen 24 uur
            </p>
          </>
        )}
      </div>
    </div>
  );
}

function Field({ id, type = 'text', placeholder, value, onChange, onBlur, error, touched, inputMode, autoComplete, ariaLabel }) {
  const invalid = !!(touched && error);
  return (
    <div className="qw-field">
      <input
        id={id}
        className={'qw-input' + (invalid ? ' is-invalid' : '')}
        type={type}
        inputMode={inputMode}
        autoComplete={autoComplete}
        placeholder={placeholder}
        aria-label={ariaLabel || placeholder}
        aria-invalid={invalid ? 'true' : 'false'}
        aria-describedby={invalid ? id + '-err' : undefined}
        value={value}
        onChange={onChange}
        onBlur={onBlur}
      />
      {invalid && <span className="qw-error" id={id + '-err'} role="alert">{error}</span>}
    </div>
  );
}

// Eerste stap: laagdrempelige dienst-keuze (multi-select chips), geen typwerk.
function DienstStep({ diensten, toggleDienst }) {
  return (
    <div className="qw-fieldset">
      <div className="qw-chips">
        {SERVICES.map(s => (
          <button
            type="button"
            key={s.id}
            className={'qw-chip' + (diensten.includes(s.id) ? ' is-on' : '')}
            aria-pressed={diensten.includes(s.id)}
            onClick={() => toggleDienst(s.id)}>
            <span className="qw-chip-ic">{s.icon}</span>
            <span className="qw-chip-label">{s.label}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

function ContactStep({ d, set, errors, touched, onBlurField, showDienst, toggleDienst }) {
  return (
    <div className="qw-form">
      <Field id="qw-naam" placeholder="Volledige naam" autoComplete="name"
        value={d.naam} onChange={e => set('naam', e.target.value)} onBlur={() => onBlurField('naam')}
        error={errors.naam} touched={touched.naam} />
      <div className="qw-row-2">
        <Field id="qw-telefoon" type="tel" inputMode="tel" autoComplete="tel" placeholder="Telefoonnummer" ariaLabel="Telefoonnummer (Nederlands, +31)"
          value={d.telefoon} onChange={e => set('telefoon', e.target.value)} onBlur={() => onBlurField('telefoon')}
          error={errors.telefoon} touched={touched.telefoon} />
        <Field id="qw-email" type="email" inputMode="email" autoComplete="email" placeholder="E-mailadres"
          value={d.email} onChange={e => set('email', e.target.value)} onBlur={() => onBlurField('email')}
          error={errors.email} touched={touched.email} />
      </div>
      <div className="qw-row-2">
        <Field id="qw-postcode" inputMode="text" autoComplete="postal-code" placeholder="Postcode" ariaLabel="Postcode, bijvoorbeeld 1234 AB"
          value={d.postcode} onChange={e => set('postcode', e.target.value)} onBlur={() => onBlurField('postcode')}
          error={errors.postcode} touched={touched.postcode} />
        <Field id="qw-adres" autoComplete="street-address" placeholder="Adres"
          value={d.adres} onChange={e => set('adres', e.target.value)} />
      </div>
      {showDienst && (
        <div className="qw-fieldset">
          <label className="qw-label">Waar kunnen we u mee helpen?</label>
          <div className="qw-chips">
            {SERVICES.map(s => (
              <button
                type="button"
                key={s.id}
                className={'qw-chip' + (d.diensten.includes(s.id) ? ' is-on' : '')}
                aria-pressed={d.diensten.includes(s.id)}
                onClick={() => toggleDienst(s.id)}>
                <span className="qw-chip-ic">{s.icon}</span>
                <span className="qw-chip-label">{s.label}</span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function OptionGrid({ options, value, onChange, columns = 2 }) {
  return (
    <div className="qw-grid" style={{ gridTemplateColumns: `repeat(${columns}, 1fr)` }}>
      {options.map(opt => (
        <button
          type="button"
          key={opt.id}
          className={'qw-tile' + (value === opt.id ? ' is-on' : '')}
          onClick={() => onChange(opt.id)}>
          {opt.icon && <span className="qw-tile-ic">{opt.icon}</span>}
          <span className="qw-tile-label">{opt.label}</span>
          {opt.sub && <span className="qw-tile-sub">{opt.sub}</span>}
          <span className="qw-tile-check">{Icon.check}</span>
        </button>
      ))}
    </div>
  );
}

function OptionList({ options, value, onChange }) {
  return (
    <div className="qw-list">
      {options.map(opt => (
        <button
          type="button"
          key={opt.id}
          className={'qw-list-item' + (value === opt.id ? ' is-on' : '')}
          onClick={() => onChange(opt.id)}>
          {opt.icon && <span className="qw-list-ic">{opt.icon}</span>}
          <span className="qw-list-text">
            <span className="qw-list-label">{opt.label}</span>
            {opt.sub && <span className="qw-list-sub">{opt.sub}</span>}
          </span>
          <span className="qw-list-radio">
            <span className="qw-list-radio-dot"></span>
          </span>
        </button>
      ))}
    </div>
  );
}

function PhotoStep({ value, onChange, onSkip, submitting }) {
  const inputRef = useRef(null);
  const onPick = (e) => {
    const file = e.target.files && e.target.files[0];
    if (file) {
      if (value && value.url) { try { URL.revokeObjectURL(value.url); } catch (_) {} }
      const url = URL.createObjectURL(file);
      onChange({ name: file.name, url });
    }
  };
  return (
    <div className="qw-photo">
      <button
        type="button"
        className="qw-drop"
        onClick={() => inputRef.current && inputRef.current.click()}>
        {value ? (
          <>
            <img src={value.url} alt="" className="qw-photo-preview" />
            <span className="qw-photo-name">{value.name}</span>
            <span className="qw-photo-replace">Andere foto kiezen</span>
          </>
        ) : (
          <>
            <span className="qw-drop-ic">{Icon.upload}</span>
            <strong>Sleep een foto hierheen</strong>
            <span>of klik om er een te kiezen</span>
          </>
        )}
        <input ref={inputRef} type="file" accept="image/*" hidden onChange={onPick} />
      </button>
      <button type="button" className="qw-skip" onClick={onSkip} disabled={submitting}>
        {submitting ? 'Versturen…' : 'Sla over'} {!submitting && <span className="qw-skip-ic">{Icon.arrow}</span>}
      </button>
    </div>
  );
}

// ───── Self-contained styles for the Harv CTA block ─────
const HARV_CTA_STYLE_ID = 'qw-harv-cta-style';
function ensureHarvCtaStyles() {
  if (typeof document === 'undefined') return;
  if (document.getElementById(HARV_CTA_STYLE_ID)) return;
  const el = document.createElement('style');
  el.id = HARV_CTA_STYLE_ID;
  el.textContent = `
    .qw-harv {
      margin-top: 22px;
      background: #4928FD;
      color: #fff;
      border-radius: 14px;
      padding: 16px;
      text-align: left;
    }
    .qw-harv-kicker {
      font-size: 12px;
      letter-spacing: .04em;
      color: rgba(255,255,255,.72);
      margin: 0 0 6px;
      font-weight: 600;
    }
    .qw-harv-title {
      font-size: 18px;
      font-weight: 700;
      color: #fff;
      margin: 0 0 4px;
      line-height: 1.2;
    }
    .qw-harv-body {
      font-size: 14px;
      color: rgba(255,255,255,.86);
      margin: 0 0 14px;
      line-height: 1.45;
    }
    .qw-harv-btn {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: #fff;
      color: #4928FD;
      font-weight: 700;
      border-radius: 10px;
      padding: 10px 16px;
      text-decoration: none;
      font-size: 14px;
      transition: transform .15s ease, box-shadow .15s ease, background-color .15s ease;
      box-shadow: 0 1px 2px rgba(0,0,0,.12);
    }
    .qw-harv-btn:hover {
      background: #f2f0ff;
      transform: translateY(-1px);
      box-shadow: 0 4px 14px rgba(0,0,0,.18);
    }
    @media (prefers-reduced-motion: reduce) {
      .qw-harv-btn { transition: none; }
      .qw-harv-btn:hover { transform: none; }
    }
  `;
  document.head.appendChild(el);
}

function buildHarvBookingUrl(d) {
  const base = 'https://cal.com/harv-agency/20min.nicolas';
  const params = [];
  const naam = (d.naam || '').trim();
  const email = (d.email || '').trim();
  if (naam) params.push('name=' + encodeURIComponent(naam));
  if (email) params.push('email=' + encodeURIComponent(email));
  return params.length ? base + '?' + params.join('&') : base;
}

function Confirmation({ d }) {
  const company = getCompanyName();
  const voornaam = d.naam.split(' ')[0] || '';
  const findLabel = (arr, id) => {
    const o = arr.find(x => x.id === id);
    return o ? o.label : null;
  };
  ensureHarvCtaStyles();
  const harvUrl = buildHarvBookingUrl(d);
  return (
    <div className="qw-done">
      <div className="qw-done-ic">{Icon.check}</div>
      <h3 className="qw-done-title">
        Gefeliciteerd, {voornaam}!
      </h3>
      <p className="qw-done-body">
        Uw aanvraag is compleet. {company} neemt binnen 24 uur contact met u op.
      </p>
      <div className="qw-summary">
        {d.diensten.length > 0 && <span className="qw-summary-pill">{d.diensten.length === 1 ? findLabel(SERVICES, d.diensten[0]) : d.diensten.length + ' diensten'}</span>}
        {d.daktype && <span className="qw-summary-pill">{findLabel(DAKTYPES, d.daktype)}</span>}
        {d.grootte && <span className="qw-summary-pill">{findLabel(GROOTTE, d.grootte)}</span>}
        {d.timing && <span className="qw-summary-pill">{findLabel(TIMING, d.timing)}</span>}
      </div>

      <div className="qw-harv">
        <p className="qw-harv-kicker">✶ Presented by Harv Agency</p>
        <h4 className="qw-harv-title">Bevalt deze site?</h4>
        <p className="qw-harv-body">Laat Harv hem voor u bouwen, met dezelfde snelle aanpak.</p>
        <a className="qw-harv-btn" href={harvUrl} target="_blank" rel="noopener">
          Plan een afspraak →
        </a>
      </div>
    </div>
  );
}

// ───── Mount ─────
// Hoofd-widget (volgorde uit de globale vlag: engage-first op origineel,
// contact-first op helder).
// combinedContact via window.__qwCombinedContact (per template): contact + dienstvraag samen op pagina 1.
const qwMain = document.getElementById('quote-widget-mount');
if (qwMain) ReactDOM.createRoot(qwMain).render(<QuoteWidget combinedContact={typeof window !== 'undefined' && !!window.__qwCombinedContact} />);
// Optionele tweede widget onderaan de pagina: altijd contact-first ("vraag eerst
// de contactgegevens"). Alleen aanwezig op templates die dit mountpunt hebben
// (origineel); helder heeft 'm niet en blijft dus één widget.
const qwBottom = document.getElementById('quote-widget-mount-bottom');
if (qwBottom) ReactDOM.createRoot(qwBottom).render(<QuoteWidget engageFirst={false} combinedContact={true} />);
