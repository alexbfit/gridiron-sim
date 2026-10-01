/* Fills [data-cfg="KEY"] text and [data-cfg-mail="KEY"] mailto links from config.js (LEGAL_ENTITY, CONTACT_EMAIL).
   Used by terms.html, privacy.html and responsible.html so the legal entity name lives in one place. */
(function () {
  const cfg = window.GRIDIRON_CONFIG || {};
  document.querySelectorAll("[data-cfg]").forEach(el => { const v = cfg[el.dataset.cfg]; if (v) el.textContent = v; });
  document.querySelectorAll("[data-cfg-mail]").forEach(el => {
    const v = cfg[el.dataset.cfgMail];
    if (v) { el.href = "mailto:" + v; el.textContent = v; } else { el.removeAttribute("href"); el.textContent = "email us"; }
  });
})();
