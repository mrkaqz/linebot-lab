// Vanilla JS, no build step, no CDN -- this has to run on a Pi and may be
// used offline. Small, page-specific initializers, called explicitly from
// each template (rather than sniffing the DOM globally) so it's obvious
// what runs where.
window.lineAdmin = (function () {
  "use strict";

  async function postForm(url, data) {
    const body = new URLSearchParams(data);
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body,
    });
    let json = null;
    try {
      json = await resp.json();
    } catch (e) {
      /* non-JSON response */
    }
    return { ok: resp.ok, status: resp.status, json: json };
  }

  // ---- Setup > LINE: group auto-detect polling ----
  function initGroupDetect() {
    const startBtn = document.getElementById("detect-start");
    const statusEl = document.getElementById("detect-status");
    const groupInput = document.getElementById("group_id");
    if (!startBtn) return;

    let poller = null;

    function poll() {
      fetch("/setup/line/detect/status")
        .then((r) => r.json())
        .then((data) => {
          statusEl.hidden = false;
          if (data.group_id) {
            statusEl.textContent = "Detected group id: " + data.group_id + (data.group_name ? " (" + data.group_name + ")" : "");
            groupInput.value = data.group_id;
            stopPolling();
          } else if (data.listening) {
            statusEl.textContent = "Listening for a message in the group... post any message now.";
          } else {
            statusEl.textContent = "Not listening. Click Detect group to try again.";
            stopPolling();
          }
        })
        .catch(() => {});
    }

    function stopPolling() {
      if (poller) {
        clearInterval(poller);
        poller = null;
      }
    }

    startBtn.addEventListener("click", function () {
      postForm("/setup/line/detect/start", {}).then(() => {
        statusEl.hidden = false;
        statusEl.textContent = "Listening for a message in the group...";
        stopPolling();
        poller = setInterval(poll, 3000);
      });
    });
  }

  // ---- Setup > OCR: test backend ----
  function initOcrTest() {
    const btn = document.getElementById("ocr-test-btn");
    const resultEl = document.getElementById("ocr-test-result");
    if (!btn) return;

    btn.addEventListener("click", function () {
      const backend = document.querySelector('input[name="backend"]:checked');
      if (!backend) return;
      const backendValue = backend.value;
      let keyFieldId = null;
      if (backendValue === "claude") keyFieldId = "anthropic_api_key";
      if (backendValue === "gemini") keyFieldId = "gemini_api_key";
      const apiKey = keyFieldId ? (document.getElementById(keyFieldId).value || "") : "";

      resultEl.hidden = false;
      resultEl.textContent = "Testing...";
      postForm("/setup/ocr/test", { backend: backendValue, api_key: apiKey }).then(({ json }) => {
        if (!json) {
          resultEl.textContent = "Test failed: unexpected response.";
          return;
        }
        if (json.ok) {
          resultEl.innerHTML =
            "<strong>Success.</strong> OPD number found: " +
            (json.opd_number || "(none)") +
            "<pre>" +
            (json.text || "").replace(/</g, "&lt;") +
            "</pre>";
        } else {
          resultEl.textContent = "Failed: " + json.error;
        }
      });
    });
  }

  // ---- Setup > General: regex tester ----
  function initRegexTest() {
    const btn = document.getElementById("regex-test-btn");
    const resultEl = document.getElementById("regex-test-result");
    if (!btn) return;

    btn.addEventListener("click", function () {
      const pattern = document.getElementById("regex-pattern-input").value;
      const sample = document.getElementById("regex-sample").value;
      resultEl.hidden = false;
      resultEl.textContent = "Testing...";
      postForm("/setup/general/test-regex", { pattern: pattern, sample_text: sample }).then(({ json }) => {
        if (!json) {
          resultEl.textContent = "Test failed: unexpected response.";
          return;
        }
        if (!json.ok) {
          resultEl.textContent = "Invalid regex: " + json.error;
          return;
        }
        if (json.matches.length === 0) {
          resultEl.textContent = "No matches.";
          return;
        }
        resultEl.innerHTML = json.matches
          .map((m) => "match: " + escapeHtml(m.match) + " -> group 1: " + escapeHtml(m.group1 || ""))
          .join("<br>");
      });
    });
  }

  function escapeHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // ---- Setup > OneDrive: folder picker ----
  function initFolderPicker() {
    const listEl = document.getElementById("picker-list");
    const crumbEl = document.getElementById("picker-breadcrumb");
    if (!listEl) return;

    let stack = [{ id: null, name: "OneDrive" }];

    function renderBreadcrumb() {
      crumbEl.innerHTML = "";
      stack.forEach((entry, idx) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "secondary";
        btn.textContent = entry.name + (idx < stack.length - 1 ? " /" : "");
        btn.addEventListener("click", function () {
          stack = stack.slice(0, idx + 1);
          load();
        });
        crumbEl.appendChild(btn);
      });
    }

    function currentPath() {
      return "/" + stack.slice(1).map((e) => e.name).join("/");
    }

    function load() {
      renderBreadcrumb();
      const current = stack[stack.length - 1];
      const url = current.id ? "/setup/onedrive/browse?item_id=" + encodeURIComponent(current.id) : "/setup/onedrive/browse";
      listEl.innerHTML = "<li>Loading...</li>";
      fetch(url)
        .then((r) => r.json())
        .then((data) => {
          listEl.innerHTML = "";
          if (!data.ok) {
            listEl.innerHTML = "<li>" + escapeHtml(data.error || "failed to load") + "</li>";
            return;
          }
          if (data.folders.length === 0) {
            listEl.innerHTML = "<li class=\"muted\">(no subfolders)</li>";
          }
          data.folders.forEach((folder) => {
            const li = document.createElement("li");
            li.textContent = "📁 " + folder.name;
            li.addEventListener("click", function () {
              stack.push({ id: folder.id, name: folder.name });
              load();
            });
            listEl.appendChild(li);
          });
        });
    }

    const newFolderBtn = document.getElementById("new-folder-btn");
    newFolderBtn.addEventListener("click", function () {
      const nameInput = document.getElementById("new-folder-name");
      const name = nameInput.value.trim();
      if (!name) return;
      const current = stack[stack.length - 1];
      postForm("/setup/onedrive/new-folder", { parent_item_id: current.id || "", name: name }).then(({ json }) => {
        if (json && json.ok) {
          nameInput.value = "";
          load();
        } else {
          alert("Could not create folder: " + (json ? json.error : "unknown error"));
        }
      });
    });

    document.getElementById("select-folder-btn").addEventListener("click", function () {
      const current = stack[stack.length - 1];
      if (!current.id) {
        alert("Pick a specific folder (not the OneDrive root) to file into.");
        return;
      }
      document.getElementById("select-item-id").value = current.id;
      document.getElementById("select-path").value = currentPath();
      document.getElementById("select-folder-form").submit();
    });

    load();
  }

  // ---- Setup > OneDrive: copy-to-clipboard for the derived URLs ----
  function initCopyButtons() {
    document.querySelectorAll("[data-copy-target]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const target = document.querySelector(btn.getAttribute("data-copy-target"));
        if (!target) return;
        const text = target.textContent || "";
        const originalLabel = btn.textContent;
        const onCopied = function () {
          btn.textContent = "Copied!";
          setTimeout(function () {
            btn.textContent = originalLabel;
          }, 1500);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(onCopied, function () {
            fallbackCopy(text, onCopied);
          });
        } else {
          fallbackCopy(text, onCopied);
        }
      });
    });
  }

  function fallbackCopy(text, done) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    try {
      document.execCommand("copy");
    } catch (e) {
      /* best effort */
    }
    document.body.removeChild(ta);
    done();
  }

  // ---- Unfiled queue: click a photo to see it full page ----
  function initPhotoLightbox() {
    const links = document.querySelectorAll("[data-lightbox]");
    if (!links.length) return;

    const box = document.createElement("div");
    box.className = "lightbox";
    box.hidden = true;
    box.innerHTML =
      '<button type="button" class="lightbox-close" aria-label="Close">&times;</button>' +
      '<img alt="lab result photo, full size">' +
      '<div class="lightbox-caption">Click anywhere or press Esc to close</div>';
    document.body.appendChild(box);

    const img = box.querySelector("img");
    let lastFocused = null;

    function open(src) {
      lastFocused = document.activeElement;
      img.src = src;
      box.hidden = false;
      document.body.style.overflow = "hidden";
      box.querySelector(".lightbox-close").focus();
    }

    function close() {
      box.hidden = true;
      img.removeAttribute("src");
      document.body.style.overflow = "";
      if (lastFocused) lastFocused.focus();
    }

    links.forEach(function (link) {
      link.addEventListener("click", function (ev) {
        // Let ctrl/cmd/middle-click still open the raw image in a new tab.
        if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button !== 0) return;
        ev.preventDefault();
        open(link.getAttribute("href"));
      });
    });

    // Clicking the image itself should not close it -- only the backdrop.
    img.addEventListener("click", function (ev) { ev.stopPropagation(); });
    box.addEventListener("click", close);
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape" && !box.hidden) close();
    });
  }

  return {
    initGroupDetect: initGroupDetect,
    initOcrTest: initOcrTest,
    initRegexTest: initRegexTest,
    initFolderPicker: initFolderPicker,
    initCopyButtons: initCopyButtons,
    initPhotoLightbox: initPhotoLightbox,
  };
})();
