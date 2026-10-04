/**
 * React-matched splash + home entry for parking-ui.
 * Hands off to app.js via window.ParkingUiEntry.startGuidance(bay).
 */
(function () {
  const SPLASH_MS = 2600;
  const BAY_PATTERN = /^[A-Z0-9]+(?:-[A-Z0-9]+)*$/;
  const QR_PARAMS = ["bay", "parking", "p", "spot", "plate", "slot"];
  const RECENT_KEY = "ms_fmc_recent";

  const MSG = {
    en: {
      powered: "Powered by",
      title: "Find My Car",
      subtitle: "AR guidance to your parking bay at Msheireb Downtown Doha",
      scan: "Scan Parking QR Code",
      or: "or enter bay number",
      bayLabel: "Parking bay / slot ID",
      ph: "e.g. 87-04C or B045",
      start: "Start AR Guidance",
      hint1: "Your camera opens and arrows on the floor guide you to your car.",
      hint2: "Allow camera and motion access when asked.",
      f1: "Scan or type",
      f2: "Live AR arrows",
      f3: "Voice guidance",
      recent: "Recent",
      slots: "Available slots",
      invalid: "Enter a valid slot ID, for example 87-04C.",
      detected: "Bay {{bay}} detected. Starting AR guidance…",
      langToggle: "عربي",
      altTitle: "اعثر على سيارتك",
    },
    ar: {
      powered: "مدعم من",
      title: "اعثر على سيارتك",
      subtitle: "إرشاد بالواقع المعزز إلى موقف سيارتك في مشيرب قلب الدوحة",
      scan: "امسح رمز QR للموقف",
      or: "أو أدخل رقم الموقف",
      bayLabel: "رقم الموقف / المعرف",
      ph: "مثال: 87-04C أو B045",
      start: "ابدأ الإرشاد بالواقع المعزز",
      hint1: "تفتح الكاميرا وتظهر أسهم على الأرض ترشدك إلى سيارتك.",
      hint2: "اسمح بالوصول إلى الكاميرا والحركة عند الطلب.",
      f1: "امسح أو اكتب",
      f2: "أسهم واقع معزز",
      f3: "إرشاد صوتي",
      recent: "الأخيرة",
      slots: "المواقف المتاحة",
      invalid: "أدخل رقم موقف صالح، مثل 87-04C.",
      detected: "تم اكتشاف الموقف {{bay}}. جارٍ بدء الإرشاد…",
      langToggle: "English",
      altTitle: "Find My Car",
    },
  };

  let lang = localStorage.getItem("ms_fmc_lang") || "en";
  if (lang !== "ar" && lang !== "en") lang = "en";
  let slotIds = [];

  const el = {
    shell: document.getElementById("shell"),
    splash: document.getElementById("splash"),
    home: document.getElementById("homeScreen"),
    arStage: document.getElementById("arStage"),
    langBtn: document.getElementById("langBtn"),
    bayInput: document.getElementById("bayInput"),
    startBtn: document.getElementById("homeStartBtn"),
    scanBtn: document.getElementById("homeScanBtn"),
    err: document.getElementById("homeErr"),
    recent: document.getElementById("homeRecent"),
    chips: document.getElementById("homeChips"),
    slots: document.getElementById("homeSlots"),
    slotChips: document.getElementById("homeSlotChips"),
    slotsTitle: document.getElementById("homeSlotsTitle"),
    toast: document.getElementById("homeToast"),
    title: document.getElementById("homeTitle"),
    arSub: document.getElementById("homeArSub"),
    sub: document.getElementById("homeSub"),
    scanLabel: document.getElementById("homeScanLabel"),
    or: document.getElementById("homeOr"),
    bayLabel: document.getElementById("homeBayLabel"),
    hint: document.getElementById("homeHint"),
    f1: document.getElementById("homeF1"),
    f2: document.getElementById("homeF2"),
    f3: document.getElementById("homeF3"),
    recentTitle: document.getElementById("homeRecentTitle"),
    powered: document.getElementById("homePowered"),
    spTitle: document.getElementById("spTitle"),
    spPowered: document.getElementById("spPowered"),
    startLabel: document.getElementById("homeStartLabel"),
  };

  function t(key, vars) {
    let s = (MSG[lang] && MSG[lang][key]) || MSG.en[key] || key;
    if (vars) {
      for (const [k, v] of Object.entries(vars)) {
        s = s.replace(new RegExp(`{{${k}}}`, "g"), String(v));
      }
    }
    return s;
  }

  function normalizeBay(v) {
    return (v || "")
      .toUpperCase()
      .trim()
      .replace(/\s+/g, "-")
      .replace(/[^A-Z0-9-]/g, "");
  }

  function validBay(v) {
    return v.length >= 2 && BAY_PATTERN.test(v);
  }

  function getRecent() {
    try {
      return JSON.parse(localStorage.getItem(RECENT_KEY) || "[]").filter(validBay);
    } catch {
      return [];
    }
  }

  function saveRecent(b) {
    try {
      const next = [b, ...getRecent().filter((x) => x !== b)].slice(0, 4);
      localStorage.setItem(RECENT_KEY, JSON.stringify(next));
    } catch {
      /* ignore */
    }
  }

  function toast(msg) {
    if (!el.toast) return;
    el.toast.textContent = msg;
    el.toast.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.toast.classList.remove("show"), 2600);
  }

  function paintI18n() {
    const dir = lang === "ar" ? "rtl" : "ltr";
    document.documentElement.lang = lang;
    document.documentElement.dir = dir;
    if (el.shell) {
      el.shell.lang = lang;
      el.shell.dir = dir;
    }
    if (el.langBtn) el.langBtn.textContent = t("langToggle");
    if (el.title) el.title.textContent = t("title");
    if (el.arSub) {
      el.arSub.textContent = t("altTitle");
      el.arSub.style.fontFamily = lang === "ar" ? "var(--f-display)" : "var(--f-ar)";
    }
    if (el.sub) el.sub.textContent = t("subtitle");
    if (el.scanLabel) el.scanLabel.textContent = t("scan");
    if (el.or) el.or.textContent = t("or");
    if (el.bayLabel) el.bayLabel.textContent = t("bayLabel");
    if (el.bayInput) el.bayInput.placeholder = t("ph");
    if (el.startLabel) el.startLabel.textContent = t("start");
    if (el.hint) el.hint.innerHTML = `${t("hint1")}<br>${t("hint2")}`;
    if (el.f1) el.f1.textContent = t("f1");
    if (el.f2) el.f2.textContent = t("f2");
    if (el.f3) el.f3.textContent = t("f3");
    if (el.recentTitle) el.recentTitle.textContent = t("recent");
    if (el.slotsTitle) el.slotsTitle.textContent = t("slots");
    if (el.powered) el.powered.textContent = t("powered");
    if (el.spTitle) {
      el.spTitle.innerHTML = `${t("title")}<small>${t("altTitle")}</small>`;
    }
    if (el.spPowered) el.spPowered.textContent = t("powered");
    paintSlots();
    paintRecent();
    syncStart();
  }

  function paintSlots() {
    if (!el.slots || !el.slotChips) return;
    const ids = slotIds.filter(validBay);
    if (!ids.length) {
      el.slots.hidden = true;
      el.slotChips.innerHTML = "";
      return;
    }
    el.slots.hidden = false;
    const picked = normalizeBay(el.bayInput?.value || "");
    el.slotChips.innerHTML = "";
    for (const id of ids) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "chip" + (id === picked ? " is-picked" : "");
      btn.textContent = id;
      btn.addEventListener("click", () => {
        if (el.bayInput) el.bayInput.value = id;
        if (el.err) el.err.textContent = "";
        syncStart();
        paintSlots();
      });
      el.slotChips.appendChild(btn);
    }
  }

  function setSlots(list) {
    slotIds = (list || [])
      .map((s) => normalizeBay(typeof s === "string" ? s : s?.slot_id || ""))
      .filter(Boolean);
    // unique, keep order
    slotIds = [...new Set(slotIds)];
    paintSlots();
  }

  function paintRecent() {
    const list = getRecent();
    if (!el.recent || !el.chips) return;
    if (!list.length) {
      el.recent.hidden = true;
      el.chips.innerHTML = "";
      return;
    }
    el.recent.hidden = false;
    el.chips.innerHTML = "";
    for (const b of list) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "chip";
      btn.textContent = b;
      btn.addEventListener("click", () => go(b));
      el.chips.appendChild(btn);
    }
  }

  function syncStart() {
    const bay = normalizeBay(el.bayInput?.value || "");
    if (el.startBtn) el.startBtn.disabled = !validBay(bay);
  }

  function hideSplash() {
    el.splash?.classList.add("out");
  }

  function showHome() {
    document.body.classList.add("entry-mode");
    if (el.shell) el.shell.hidden = false;
    if (el.arStage) el.arStage.hidden = true;
    el.home?.classList.add("active");
  }

  function enterGuidance(bay) {
    saveRecent(bay);
    document.body.classList.remove("entry-mode");
    if (el.shell) el.shell.hidden = true;
    if (el.arStage) el.arStage.hidden = false;
    if (typeof window.ParkingUiEntry?.startGuidance === "function") {
      window.ParkingUiEntry.startGuidance(bay);
    } else {
      const slot = document.getElementById("slotInput");
      if (slot) {
        slot.value = bay;
        slot.dispatchEvent(new Event("input", { bubbles: true }));
      }
    }
  }

  function go(raw) {
    const bay = normalizeBay(raw);
    if (!validBay(bay)) {
      if (el.err) el.err.textContent = t("invalid");
      return;
    }
    if (el.err) el.err.textContent = "";
    enterGuidance(bay);
  }

  function readQrParams() {
    const params = new URLSearchParams(window.location.search);
    for (const k of QR_PARAMS) {
      const v = normalizeBay(params.get(k) || "");
      if (v && validBay(v)) {
        if (el.bayInput) el.bayInput.value = v;
        toast(t("detected", { bay: v }));
        syncStart();
        break;
      }
    }
  }

  // expose for app.js / scan
  window.ParkingHome = {
    go,
    showHome,
    toast,
    setSlots,
    normalizeBay,
    validBay,
    t: (k, v) => t(k, v),
    getLang: () => lang,
  };

  el.splash?.addEventListener("click", hideSplash);
  el.langBtn?.addEventListener("click", () => {
    lang = lang === "ar" ? "en" : "ar";
    localStorage.setItem("ms_fmc_lang", lang);
    paintI18n();
  });
  el.bayInput?.addEventListener("input", () => {
    const v = normalizeBay(el.bayInput.value);
    if (el.bayInput.value !== v) el.bayInput.value = v;
    if (el.err) el.err.textContent = "";
    syncStart();
    paintSlots();
  });
  el.bayInput?.addEventListener("keydown", (evt) => {
    if (evt.key === "Enter") {
      evt.preventDefault();
      go(el.bayInput.value);
    }
  });
  el.startBtn?.addEventListener("click", () => go(el.bayInput?.value || ""));
  el.scanBtn?.addEventListener("click", () => {
    // focus bay entry — full QR scanner can plug in later
    el.bayInput?.focus();
    toast(lang === "ar" ? "أدخل رقم الموقف أو استخدم رابط QR" : "Enter bay number or open a parking QR link");
  });

  paintI18n();
  readQrParams();
  showHome();

  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const ms = reduce ? 600 : SPLASH_MS;
  setTimeout(hideSplash, ms);
})();
