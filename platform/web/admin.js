/* Opcare DAQ – Admin tab: users (add, role, reset password, disable, delete) and the audit log.
   Only admins see the tab; the API enforces the same rule. Plain JavaScript, no build step. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  function h(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (v === undefined || v === null || v === false) return;
      if (k === "class") e.className = v;
      else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else if (k === "value") e.value = v;
      else e.setAttribute(k, v === true ? "" : v);
    });
    kids.flat().forEach((c) => { if (c !== null && c !== undefined && c !== false) e.append(c instanceof Node ? c : String(c)); });
    return e;
  }
  const token = () => { try { return sessionStorage.getItem("opcare-token"); } catch (e) { return null; } };
  async function api(path, method = "GET", body) {
    const res = await fetch(path, {method, headers: {"Authorization": `Bearer ${token()}`, "Content-Type": "application/json"},
      body: body === undefined ? undefined : JSON.stringify(body)});
    let data = null; try { data = res.status === 204 ? null : await res.json(); } catch (e) { data = null; }
    return {status: res.status, ok: res.ok, data};
  }
  const msg = (r) => { const m = r.data?.error?.message || `HTTP ${r.status}`; return m.charAt(0).toUpperCase() + m.slice(1) + "."; };
  function toast(text, kind = "ok") {
    const t = $("toast"); t.textContent = text; t.dataset.kind = kind; t.hidden = false;
    clearTimeout(toast.t); toast.t = setTimeout(() => { t.hidden = true; }, 5000);
  }
  const fmt = (iso) => iso ? new Date(iso).toLocaleString([], {dateStyle: "medium", timeStyle: "short"}) : "never";
  const ROLE_ORDER = ["viewer", "operator", "engineer", "admin"];
  const ACTION_TEXT = {login: "Signed in", logout: "Signed out", login_failed: "Failed sign-in", change_password: "Changed own password",
    create_user: "Added user", update_user: "Changed user", reset_password: "Reset password", delete_user: "Deleted user",
    create_source: "Added source", update_source: "Changed source", delete_source: "Deleted source", store_secret: "Stored device password"};

  function generatePassword() {
    const abc = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz", dig = "23456789", all = abc + dig;
    const r = new Uint32Array(14); crypto.getRandomValues(r);
    const chars = [...r].map((x) => all[x % all.length]);
    chars[3] = dig[r[3] % dig.length]; chars[9] = dig[r[9] % dig.length]; chars[0] = abc[r[0] % abc.length];
    return chars.join("");
  }

  let state = {users: [], roles: {}, adding: false, resetting: null, deleting: null, formError: null, audit: []};
  const me = () => window.opcareMe || {};

  async function open() {
    const root = $("view-admin");
    if (me().role && me().role !== "admin") { root.replaceChildren(h("p", {class: "error"}, "Only administrators can manage users.")); return; }
    const [u, a] = await Promise.all([api("/api/users"), api("/api/audit")]);
    if (!u.ok) { root.replaceChildren(h("p", {class: "error"}, msg(u))); return; }
    state.users = u.data.users; state.roles = u.data.roles; state.audit = a.ok ? a.data : [];
    render();
  }

  function render() {
    const root = $("view-admin");
    root.replaceChildren(
      h("div", {class: "page-head"}, h("div", {}, h("h1", {}, "Administration"),
        h("p", {class: "muted"}, "Who can sign in to the platform, what each person may do, and what was changed."))),
      h("div", {class: "admin-grid"}, usersPanel(), auditPanel()));
  }

  // ------------------------------------------------------------------ users
  function usersPanel() {
    const active = state.users.filter((u) => u.active).length;
    return h("section", {class: "panel", "aria-labelledby": "h-users"},
      h("div", {class: "panel-head"}, h("h2", {id: "h-users"}, `Users · ${state.users.length} (${active} active)`),
        state.adding ? null : h("button", {type: "button", class: "btn primary", id: "add-user", onclick: () => { state.adding = true; state.formError = null; render(); $("u-name")?.focus(); }}, "Add user")),
      state.adding ? addForm() : null,
      h("div", {class: "table-wrap plain"}, h("table", {class: "grid users", id: "users-table"},
        h("thead", {}, h("tr", {}, ["User", "Role", "Status", "Last sign-in", ""].map((x) => h("th", {scope: "col"}, x)))),
        h("tbody", {}, state.users.map(userRow)))),
      h("div", {class: "roles"}, ROLE_ORDER.map((r) => h("div", {}, h("b", {}, r), " — ", state.roles[r] || ""))));
  }

  function addForm() {
    const pw = state.newPassword = state.newPassword || generatePassword();
    return h("form", {class: "user-form", id: "user-form", onsubmit: createUser},
      h("label", {}, "Full name", h("input", {id: "u-name", required: true, placeholder: "e.g. Ahmed Hassan", maxlength: 100})),
      h("label", {}, "User name (to sign in)", h("input", {id: "u-user", required: true, pattern: "[a-z0-9][a-z0-9._\\-]{1,31}", placeholder: "e.g. ahmed.hassan",
        autocapitalize: "none", spellcheck: "false", title: "2-32 lower case letters, digits, dot, dash or underscore"})),
      h("label", {}, "E-mail (optional)", h("input", {id: "u-email", type: "email", maxlength: 200})),
      h("label", {}, "Role", h("select", {id: "u-role"}, ROLE_ORDER.map((r) => { const o = h("option", {value: r}, r[0].toUpperCase() + r.slice(1)); if (r === "viewer") o.selected = true; return o; }))),
      h("label", {}, "Temporary password", h("input", {id: "u-pass", required: true, minlength: 10, value: pw, autocomplete: "off"})),
      h("label", {}, " ", h("button", {type: "button", class: "btn", onclick: () => { state.newPassword = generatePassword(); $("u-pass").value = state.newPassword; }}, "New random password")),
      h("p", {class: "muted small", style: "grid-column: 1 / -1; margin: 0"},
        "Give the user name and temporary password to the person. They must choose their own password the first time they sign in."),
      state.formError ? h("p", {class: "error", role: "alert", style: "grid-column: 1 / -1; margin: 0"}, state.formError) : null,
      h("div", {class: "actions", style: "grid-column: 1 / -1"},
        h("button", {type: "submit", class: "btn primary", id: "u-create"}, "Create user"),
        h("button", {type: "button", class: "btn ghost", onclick: () => { state.adding = false; state.newPassword = null; render(); }}, "Cancel")));
  }

  async function createUser(ev) {
    ev.preventDefault();
    const body = {name: $("u-name").value.trim(), username: $("u-user").value.trim().toLowerCase(), email: $("u-email").value.trim() || undefined,
      role: $("u-role").value, password: $("u-pass").value, must_change_password: true};
    const r = await api("/api/users", "POST", body);
    if (!r.ok) { state.formError = msg(r); render(); return; }
    state.adding = false; state.newPassword = null; state.formError = null;
    toast(`${r.data.name} (${r.data.username}) can now sign in as ${r.data.role}.`);
    await open();
  }

  function userRow(u) {
    const self = u.username === me().user;
    const status = [h("span", {class: `chip ${u.active ? "on" : "off"}`}, u.active ? "Active" : "Disabled")];
    if (u.must_change_password && u.active) status.push(" ", h("span", {class: "chip must", title: "Has a temporary password"}, "New password due"));
    const role = h("select", {"aria-label": `Role of ${u.username}`, disabled: self, onchange: (e) => update(u, {role: e.target.value})},
      ROLE_ORDER.map((r) => { const o = h("option", {value: r}, r[0].toUpperCase() + r.slice(1)); if (r === u.role) o.selected = true; return o; }));
    let actions;
    if (state.resetting === u.username) {
      const pw = state.resetPassword = state.resetPassword || generatePassword();
      actions = [h("input", {id: "reset-pass", value: pw, "aria-label": "New temporary password", style: "width:150px", class: "mono"}), " ",
        h("button", {type: "button", class: "btn primary", onclick: () => resetPassword(u)}, "Set password"), " ",
        h("button", {type: "button", class: "btn ghost", onclick: () => { state.resetting = null; state.resetPassword = null; render(); }}, "Cancel")];
    } else if (state.deleting === u.username) {
      actions = [h("span", {class: "confirm"}, `Delete ${u.username}? This cannot be undone. `),
        h("button", {type: "button", class: "btn danger", onclick: () => remove(u)}, "Delete"), " ",
        h("button", {type: "button", class: "btn ghost", onclick: () => { state.deleting = null; render(); }}, "Cancel")];
    } else {
      actions = [h("button", {type: "button", class: "btn", onclick: () => { state.resetting = u.username; state.resetPassword = null; render(); }}, "Reset password"), " ",
        self ? null : h("button", {type: "button", class: "btn", onclick: () => update(u, {active: !u.active})}, u.active ? "Disable" : "Enable"), " ",
        self ? null : h("button", {type: "button", class: "btn danger-ghost", onclick: () => { state.deleting = u.username; render(); }}, "Delete")];
    }
    return h("tr", {"data-user": u.username},
      h("td", {class: "who"}, h("b", {}, u.name, self ? h("span", {class: "you"}, "you") : null), h("small", {}, u.username + (u.email ? ` · ${u.email}` : ""))),
      h("td", {}, role), h("td", {}, status), h("td", {}, fmt(u.last_login)), h("td", {class: "actions"}, actions));
  }

  async function update(u, change) {
    const r = await api(`/api/users/${encodeURIComponent(u.username)}`, "PUT", {name: u.name, email: u.email, role: u.role, active: u.active, ...change});
    if (!r.ok) { toast(msg(r), "error"); await open(); return; }
    toast(change.role ? `${u.username} is now ${change.role}.` : `${u.username} ${change.active ? "can sign in again" : "is disabled and signed out"}.`);
    await open();
  }

  async function resetPassword(u) {
    const password = $("reset-pass").value;
    const r = await api(`/api/users/${encodeURIComponent(u.username)}/password`, "POST", {password});
    if (!r.ok) { toast(msg(r), "error"); return; }
    state.resetting = null; state.resetPassword = null;
    toast(u.username === me().user ? "Your password was changed." : `New temporary password for ${u.username}: ${password} — they choose their own at next sign-in.`);
    await open();
  }

  async function remove(u) {
    const r = await api(`/api/users/${encodeURIComponent(u.username)}`, "DELETE");
    state.deleting = null;
    if (r.status !== 204) { toast(msg(r), "error"); await open(); return; }
    toast(`${u.username} was deleted.`);
    await open();
  }

  // ------------------------------------------------------------------ audit log
  function auditPanel() {
    const rows = state.audit.slice(0, 100).map((a) => h("tr", {},
      h("td", {}, fmt(a.ts)), h("td", {}, a.user), h("td", {}, ACTION_TEXT[a.action] || a.action), h("td", {}, a.target), h("td", {class: "muted"}, a.detail || "")));
    return h("section", {class: "panel", "aria-labelledby": "h-audit"},
      h("div", {class: "panel-head"}, h("h2", {id: "h-audit"}, "Audit log"),
        h("span", {class: "muted small"}, "Sign-ins, user changes and configuration changes. Passwords are never recorded.")),
      h("div", {class: "table-wrap"}, h("table", {class: "grid audit", id: "audit-table"},
        h("thead", {}, h("tr", {}, ["When", "Who", "What", "On", "Details"].map((x) => h("th", {scope: "col"}, x)))),
        h("tbody", {}, rows.length ? rows : h("tr", {}, h("td", {colspan: 5, class: "muted"}, "Nothing recorded yet."))))));
  }

  window.OpcareAdmin = {open};
})();
