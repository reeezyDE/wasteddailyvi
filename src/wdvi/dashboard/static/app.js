/* WastedDailyVI Dashboard – kleine Verbesserungen ohne Framework. Ohne JS funktioniert alles per Formular. */
(function () {
  "use strict";

  var toastTimer = null;
  function toast(text, kind) {
    var el = document.getElementById("toast");
    if (!el) return;
    el.textContent = text;
    el.className = "toast " + (kind || "");
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.hidden = true; }, kind === "error" ? 6000 : 2600);
  }

  function setPending(n) {
    if (typeof n !== "number") return;
    document.querySelectorAll("[data-pending-count]").forEach(function (el) {
      el.textContent = String(n);
      el.hidden = n <= 0;
    });
    var summary = document.querySelector("[data-review-summary]");
    if (summary) {
      summary.textContent = n > 0
        ? n + (n === 1 ? " Clip wartet auf deine Freigabe" : " Clips warten auf deine Freigabe")
        : "Keine Clips in der Warteschlange";
    }
  }

  // Bestätigungsdialoge (Abbrechen, Löschen, Trennen)
  document.addEventListener("submit", function (e) {
    var form = e.target;
    var msg = form.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) {
      e.preventDefault();
      e.stopImmediatePropagation();
    }
  }, true);

  // Freigeben / Ablehnen / Texte speichern ohne Neuladen
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form.hasAttribute("data-ajax") || !window.fetch || !window.FormData) return;
    if (!("submitter" in e)) return; // alter Browser: normal absenden (formaction bleibt korrekt)
    var submitter = e.submitter || null;
    var action = (submitter && submitter.getAttribute("formaction")) || form.getAttribute("action");
    var kind = (submitter && submitter.getAttribute("data-kind")) || form.getAttribute("data-kind") || "";
    var card = form.closest("[data-clip]");
    var data;
    try { data = new FormData(form); } catch (err) { return; }
    e.preventDefault();

    var buttons = card ? card.querySelectorAll("button[type=submit]") : form.querySelectorAll("button");
    buttons.forEach(function (b) { b.classList.add("busy"); });

    fetch(action, {
      method: "POST",
      body: data,
      credentials: "same-origin",
      headers: { "X-WDVI-Ajax": "1", "Accept": "application/json" }
    }).then(function (resp) {
      var ct = resp.headers.get("content-type") || "";
      if (ct.indexOf("application/json") === -1) {
        // z. B. Login abgelaufen -> klassisch absenden
        throw new Error("fallback");
      }
      return resp.json();
    }).then(function (res) {
      buttons.forEach(function (b) { b.classList.remove("busy"); });
      toast(res.message || (res.ok ? "Erledigt" : "Fehler"), res.ok ? "ok" : "error");
      if (!res.ok) return;
      setPending(res.pending);
      if (card && (kind === "approve" || kind === "reject")) {
        card.classList.add(kind === "approve" ? "done-approve" : "done-reject");
        card.querySelectorAll("video").forEach(function (v) { try { v.pause(); } catch (err) { /* egal */ } });
        setTimeout(function () { card.classList.add("leaving"); }, 250);
        setTimeout(function () {
          card.remove();
          if (!document.querySelector("[data-clip]")) window.location.reload();
        }, 550);
      }
    }).catch(function (err) {
      buttons.forEach(function (b) { b.classList.remove("busy"); });
      if (err && err.message === "fallback") {
        form.removeAttribute("data-ajax");
        if (submitter && form.requestSubmit) form.requestSubmit(submitter); else form.submit();
        return;
      }
      toast("Keine Verbindung zum Dashboard – bitte erneut versuchen.", "error");
    });
  });

  // Schnellauswahl Ablehnungsgrund
  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest("[data-reason]");
    if (!btn) return;
    var card = btn.closest("[data-clip]");
    var input = card && card.querySelector("[data-reason-input]");
    if (input) { input.value = btn.getAttribute("data-reason"); input.focus(); }
  });

  // Kopieren: navigator.clipboard gibt es nur über HTTPS/localhost -> sonst execCommand, zuletzt Text markieren
  function selectText(el) {
    try {
      var range = document.createRange();
      range.selectNodeContents(el);
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    } catch (err) { /* egal */ }
  }
  function copyFallback(text, source) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { ta.setSelectionRange(0, text.length); } catch (err) { /* egal */ }
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
    document.body.removeChild(ta);
    if (ok) { toast("Kopiert", "ok"); return; }
    if (source) {
      var details = source.closest("details");
      if (details) details.open = true;
      selectText(source);
    }
    toast("Automatisch kopieren ging nicht – Text ist markiert, bitte selbst kopieren.", "error");
  }
  function copyText(text, source) {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(
        function () { toast("Kopiert", "ok"); },
        function () { copyFallback(text, source); }
      );
    } else {
      copyFallback(text, source);
    }
  }
  document.addEventListener("click", function (e) {
    if (!e.target.closest) return;
    var btn = e.target.closest("[data-copy-target]");
    if (btn) {
      var src = document.getElementById(btn.getAttribute("data-copy-target"));
      if (src) copyText(src.textContent, src);
      return;
    }
    var el = e.target.closest("[data-copy]");
    if (el) copyText(el.textContent.trim(), el);
  });

  // Zeichenzähler
  function updateCounter(input) {
    var max = parseInt(input.getAttribute("data-max"), 10);
    var counter = document.querySelector('[data-counter-for="' + input.id + '"]');
    if (!counter || !max) return;
    var len = Array.from(input.value).length;
    counter.textContent = len + "/" + max;
    counter.classList.toggle("over", len > max);
  }
  function autogrow(t) {
    t.style.height = "auto";
    t.style.height = Math.min(t.scrollHeight + 2, 480) + "px";
  }
  document.addEventListener("input", function (e) {
    var t = e.target;
    if (t.hasAttribute && t.hasAttribute("data-max")) updateCounter(t);
    if (t.hasAttribute && t.hasAttribute("data-autogrow")) autogrow(t);
    if (t.name && t.name.indexOf("title_") === 0) {
      var block = t.closest("details");
      var prev = block && block.querySelector(".tb-preview");
      if (prev) prev.textContent = t.value || "– kein Titel –";
    }
  });
  document.addEventListener("toggle", function (e) {
    if (e.target.tagName === "DETAILS" && e.target.open) {
      e.target.querySelectorAll("[data-autogrow]").forEach(autogrow);
    }
  }, true);

  // Nur ein Video gleichzeitig abspielen
  document.addEventListener("play", function (e) {
    document.querySelectorAll("video").forEach(function (v) { if (v !== e.target) v.pause(); });
  }, true);

  function init() {
    document.querySelectorAll("[data-max]").forEach(updateCounter);
    // aktiven Tab in der scrollbaren Navigation sichtbar machen
    var tab = document.querySelector(".tabs .tab.active");
    var nav = tab && tab.parentElement;
    if (nav && tab.offsetLeft + tab.offsetWidth > nav.clientWidth) {
      nav.scrollLeft = tab.offsetLeft - (nav.clientWidth - tab.offsetWidth) / 2;
    }
    // Zeiten zusätzlich in der Zeitzone des Geräts zeigen, wenn sie abweicht
    document.querySelectorAll("time[data-utc]").forEach(function (el) {
      var d = new Date(el.getAttribute("data-utc"));
      if (isNaN(d.getTime())) return;
      var mine = d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
      if (mine !== el.textContent.trim()) {
        var span = document.createElement("span");
        span.className = "local-time";
        span.textContent = "(" + mine + " bei dir)";
        el.insertAdjacentElement("afterend", span);
      }
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
