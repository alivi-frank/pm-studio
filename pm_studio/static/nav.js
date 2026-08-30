/* The one navigation component, shared by every page.
 *
 * Loaded from <head> WITHOUT defer, on purpose: pages run their own inline scripts at
 * the end of <body>, which execute before any deferred script would. Running here means
 * `window.PMNav.auth` already exists by the time a page's inline script wants it, so
 * /auth/me is fetched exactly once per page load instead of once per consumer.
 *
 * A page opts in with one element:
 *     <div id="pm-nav" data-page="roadmap"></div>
 * `data-page` is one of the keys of PAGES below. Session-scoped pages (chat, dashboard)
 * additionally read the session id out of the URL and resolve its title, so the trail
 * says "Sessions > Checkout redesign > Chat" rather than "Sessions > 7f3a91c2".
 *
 * The destinations are listed ONCE, in the bar. An earlier version also drew a
 * Portfolio -> Roadmap -> Sessions flow map in the second row, which re-listed the same
 * three links directly under themselves; the flow now lives in the bar itself as arrows
 * BETWEEN those tabs, and the second row says only what the current page is and holds
 * whatever controls that page mounts into it (see PMNav.ready).
 */

(function () {
  "use strict";

  // The three stops of the work model, in the order intent flows through them - the
  // arrows between them in the bar are this relationship, and `what` is the one-line
  // descriptor the context row shows for whichever stop you are standing on. See
  // docs/ARCHITECTURE.md section 3b for the model this mirrors.
  // The read-first answer to "what are we working on?" - a summary of the work model,
  // not a stop on it, so it leads the bar but stays outside the arrow chain the same
  // way Systems trails it.
  var OVERVIEW_TAB = {
    page: "overview", href: "/overview", label: "Overview",
    what: "in flight · shipped recently · who's on what",
  };

  var CORE_TABS = [
    { page: "portfolio", href: "/portfolio", label: "Portfolio", what: "goals · initiatives · projects" },
    { page: "roadmap", href: "/roadmap", label: "Roadmap", what: "changes · now / next / later" },
    { page: "sessions", href: "/", label: "Sessions", what: "the work itself" },
  ];

  // Reference, not a stop on the work model - which is why it sits outside CORE_TABS and
  // its arrow chain. Systems are the technology the products are built on; they carry no
  // roadmap of their own, so they are something you look up rather than something intent
  // flows through. Shown only when the deployment declares [systems] (see /auth/me), so
  // an instance that does not use the layer is never offered a tab into an empty table.
  var REFERENCE_TABS = [
    {
      page: "systems", href: "/systems", label: "Systems",
      what: "the technology changes are contained within",
    },
  ];

  // The last group in the bar. `personal` says whether the page is reachable when there
  // is no identity at all - both of these are: Time & cost reports on "this machine", and
  // People carries the directory of who is doing the work, which a tracker sync fills in
  // whether or not anybody can sign in. The roster half of that page IS enterprise-only,
  // and the page hides it rather than the tab hiding the page: a personal instance still
  // has people working on its tickets.
  var ADMIN_TABS = [
    {
      page: "costing", href: "/costing", label: "Time & cost", capability: "view_cost", personal: true,
      what: "each person's session activity, rolled up onto the projects",
    },
    {
      page: "people", href: "/people", label: "People", personal: true,
      what: "who is doing the work — and who may see and change it",
    },
  ];

  // Per-page chrome. `tab` marks which top-level tab lights up (session-scoped pages
  // light up "Sessions", because that is where they live).
  var PAGES = {
    overview: { tab: "overview" },
    sessions: { tab: "sessions" },
    portfolio: { tab: "portfolio" },
    roadmap: { tab: "roadmap" },
    systems: { tab: "systems" },
    costing: { tab: "costing" },
    people: { tab: "people" },
    chat: { tab: "sessions", context: "session", subtab: "chat" },
    dashboard: { tab: "sessions", context: "session", subtab: "dashboard" },
  };

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function link(href, className, text) {
    var a = el("a", className, text);
    a.href = href;
    return a;
  }

  function getJSON(url) {
    // Resolves to null rather than rejecting, so callers can treat "nothing to show"
    // and "the request failed" the same way instead of branching on both.
    return fetch(url, { headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  }

  // The session id for the pages nested inside one. Derived from the path rather than
  // from `data-page`, so the fetch can start now instead of waiting for the DOM.
  var sessionId = /^\/(chat|dashboard)\/([^/]+)/.exec(location.pathname);
  sessionId = sessionId ? sessionId[2] : null;

  // Both requests are shared with the host page through window.PMNav, so a page load
  // makes one /auth/me and at most one /sessions/{id} however many consumers there are.
  var auth = getJSON("/auth/me");
  var session = sessionId ? getJSON("/sessions/" + sessionId) : Promise.resolve(null);

  // Resolves with the context row's controls slot once the nav has rendered. A page puts
  // its OWN view controls there (the roadmap's lens and view toggles, for instance)
  // instead of growing a third band of chrome below the nav.
  var resolveReady;
  var ready = new Promise(function (resolve) { resolveReady = resolve; });

  window.PMNav = { auth: auth, session: session, sessionId: sessionId, ready: ready };

  function visibleAdminTabs(info) {
    if (!info || !info.enterprise || !info.user) {
      return ADMIN_TABS.filter(function (tab) { return tab.personal; });
    }
    var can = info.capabilities || [];
    return ADMIN_TABS.filter(function (tab) {
      // Hiding a tab this role cannot use is orientation, not protection - the server
      // enforces every one of these capabilities independently.
      if (tab.capability) return can.indexOf(tab.capability) !== -1;
      if (tab.role) return info.user.role === tab.role;
      return true;
    });
  }

  function buildBar(current, info) {
    var bar = el("nav", "pmnav-bar");
    bar.setAttribute("aria-label", "Studio sections");

    bar.appendChild(link("/", "pmnav-brand", "PM Studio"));

    var tabs = el("div", "pmnav-tabs");
    tabs.appendChild(makeTab(OVERVIEW_TAB, current, "pmnav-tab"));
    tabs.appendChild(el("span", "pmnav-group-sep"));
    CORE_TABS.forEach(function (tab, i) {
      // The work model, drawn where the destinations already are: intent narrowing into
      // work. Decorative - the tabs are the navigation, the arrow is the relationship.
      if (i > 0) {
        var arrow = el("span", "pmnav-arrow", "→");
        arrow.setAttribute("aria-hidden", "true");
        tabs.appendChild(arrow);
      }
      tabs.appendChild(makeTab(
        tab.page === "sessions" ? { page: tab.page, href: sessionsHref, label: tab.label, what: tab.what } : tab,
        current, "pmnav-tab"));
    });

    if (info && info.systems_declared) {
      tabs.appendChild(el("span", "pmnav-group-sep"));
      REFERENCE_TABS.forEach(function (tab) {
        tabs.appendChild(makeTab(tab, current, "pmnav-tab"));
      });
    }

    var admin = visibleAdminTabs(info);
    if (admin.length) {
      tabs.appendChild(el("span", "pmnav-group-sep"));
      admin.forEach(function (tab) {
        tabs.appendChild(makeTab(tab, current, "pmnav-tab"));
      });
    }
    bar.appendChild(tabs);

    bar.appendChild(el("div", "pmnav-spacer"));

    // Identity and sign-out on every page, not just the sessions list: signing out used
    // to mean navigating home first.
    if (info && info.enterprise && info.user) {
      var id = el("div", "pmnav-id");
      id.appendChild(el("span", "pmnav-id-name", info.user.name));
      id.appendChild(el("span", "pmnav-role", info.user.role_label || info.user.role));
      var out = el("button", "pmnav-signout", "Sign out");
      out.type = "button";
      out.addEventListener("click", function () {
        fetch("/auth/logout", { method: "POST" }).finally(function () {
          location.href = "/login";
        });
      });
      id.appendChild(out);
      bar.appendChild(id);
    }

    return bar;
  }

  function makeTab(tab, current, className) {
    var a = link(tab.href, className, tab.label);
    if (tab.what) a.title = tab.label + " — " + tab.what;
    if (tab.page === current) a.setAttribute("aria-current", "page");
    return a;
  }

  // Row 2 for an ordinary page: the name of where you are and what it holds. No link
  // set - the bar above is the only place destinations are listed.
  function buildPageContext(spec) {
    var row = el("div", "pmnav-context");
    var all = [OVERVIEW_TAB].concat(CORE_TABS, REFERENCE_TABS, ADMIN_TABS);
    var here = null;
    all.forEach(function (tab) { if (tab.page === spec.tab) here = tab; });

    var where = el("div", "pmnav-where");
    where.appendChild(el("span", "pmnav-where-name", here ? here.label : ""));
    if (here && here.what) where.appendChild(el("span", "pmnav-where-what", here.what));
    row.appendChild(where);
    return row;
  }

  function buildSessionContext(spec) {
    var row = el("div", "pmnav-context");

    var crumbs = el("nav", "pmnav-crumbs");
    crumbs.setAttribute("aria-label", "Breadcrumb");
    crumbs.appendChild(link(sessionsHref, null, "Sessions"));
    var chev = el("span", "pmnav-chev", "›");
    chev.setAttribute("aria-hidden", "true");
    crumbs.appendChild(chev);
    // Placeholder until the title arrives; the id is at least a stable handle.
    var current = el("span", "pmnav-crumb-current", "Session " + sessionId.slice(0, 8));
    crumbs.appendChild(current);
    row.appendChild(crumbs);

    var subtabs = el("nav", "pmnav-subtabs");
    subtabs.setAttribute("aria-label", "Views of this session");
    [
      { page: "chat", href: "/chat/" + sessionId, label: "Chat" },
      { page: "dashboard", href: "/dashboard/" + sessionId, label: "Dev lifecycle" },
    ].forEach(function (tab) {
      subtabs.appendChild(makeTab(tab, spec.subtab, "pmnav-subtab"));
    });
    row.appendChild(subtabs);

    // Same title precedence the sessions list and the chat header use: the
    // PM-maintained title, then the creation name, then a placeholder. If the request
    // failed the truncated id stays in place and the trail still works.
    session.then(function (data) {
      if (!data) return;
      var title = data.title || data.name || "Untitled session";
      current.textContent = title;
      current.title = title;
    });

    return row;
  }

  // Where the sessions list lives: "/" by default, "/sessions-page" when the
  // deployment points the front door at the overview (see /auth/me `landing`).
  var sessionsHref = "/";

  function render() {
    var mount = document.getElementById("pm-nav");
    if (!mount) return;
    var spec = PAGES[mount.dataset.page];
    if (!spec) return;

    auth.then(function (info) {
      if (info && info.landing === "overview") sessionsHref = "/sessions-page";
      mount.textContent = "";
      mount.appendChild(buildBar(spec.tab, info));
      var context = spec.context === "session"
        ? buildSessionContext(spec)
        : buildPageContext(spec);
      // Always present, always last in the row, so a page's controls land right-aligned
      // whether or not it has any - and the row keeps one height either way.
      var slot = el("div", "pmnav-slot");
      slot.id = "pm-nav-slot";
      context.appendChild(slot);
      mount.appendChild(context);
      resolveReady(slot);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", render);
  } else {
    render();
  }
})();
