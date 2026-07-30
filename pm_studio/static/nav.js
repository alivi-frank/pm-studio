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
 */

(function () {
  "use strict";

  // The three stops of the work model, in the order intent flows through them. Each is
  // rendered in the context row as a map of how the pages relate to one another - see
  // docs/ARCHITECTURE.md section 3b for the model this mirrors.
  var FLOW = [
    { page: "portfolio", href: "/portfolio", name: "Portfolio", what: "goals · initiatives · projects" },
    { page: "roadmap", href: "/roadmap", name: "Roadmap", what: "changes · now / next / later" },
    { page: "sessions", href: "/", name: "Sessions", what: "the work itself" },
  ];

  // Per-page chrome. `tab` marks which top-level tab lights up (session-scoped pages
  // light up "Sessions", because that is where they live). `aside` explains a page that
  // reads the flow without being a stop on it.
  var PAGES = {
    sessions: { tab: "sessions", context: "flow" },
    portfolio: { tab: "portfolio", context: "flow" },
    roadmap: { tab: "roadmap", context: "flow" },
    costing: {
      tab: "costing",
      context: "flow",
      aside: "Time & cost rolls each person's session activity up onto the projects above.",
    },
    people: {
      tab: "people",
      context: "flow",
      aside: "People decides who may see and change everything above.",
    },
    chat: { tab: "sessions", context: "session", subtab: "chat" },
    dashboard: { tab: "sessions", context: "session", subtab: "dashboard" },
  };

  // Tabs the whole studio always has, then the two that sit behind a role.
  var CORE_TABS = [
    { page: "portfolio", href: "/portfolio", label: "Portfolio" },
    { page: "roadmap", href: "/roadmap", label: "Roadmap" },
    { page: "sessions", href: "/", label: "Sessions" },
  ];
  // `personal` says whether the page is reachable when there is no identity at all.
  // Time & cost is (it reports on "this machine"); People is not - the roster endpoints
  // are enterprise-only, so linking it from a personal instance would offer a tab whose
  // only content is an error.
  var ADMIN_TABS = [
    { page: "costing", href: "/costing", label: "Time & cost", capability: "view_cost", personal: true },
    { page: "people", href: "/people", label: "People", role: "admin", personal: false },
  ];

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

  window.PMNav = { auth: auth, session: session, sessionId: sessionId };

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
    CORE_TABS.forEach(function (tab) {
      tabs.appendChild(makeTab(tab, current, "pmnav-tab"));
    });

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
    if (tab.page === current) a.setAttribute("aria-current", "page");
    return a;
  }

  function buildFlow(spec) {
    var row = el("div", "pmnav-context");
    var list = el("ol", "pmnav-flow");
    list.setAttribute("aria-label", "How the studio's pages relate");

    FLOW.forEach(function (stop, i) {
      if (i > 0) {
        var arrow = el("li", "pmnav-arrow", "→");
        arrow.setAttribute("aria-hidden", "true");
        list.appendChild(arrow);
      }
      var item = el("li", "pmnav-step" + (stop.page === spec.tab ? " is-here" : ""));
      var a = link(stop.href, null);
      a.appendChild(el("span", "pmnav-step-name", stop.name));
      a.appendChild(el("span", "pmnav-step-what", stop.what));
      if (stop.page === spec.tab) a.setAttribute("aria-current", "page");
      item.appendChild(a);
      list.appendChild(item);
    });

    row.appendChild(list);
    if (spec.aside) row.appendChild(el("span", "pmnav-aside", spec.aside));
    return row;
  }

  function buildSessionContext(spec) {
    var row = el("div", "pmnav-context");

    var crumbs = el("nav", "pmnav-crumbs");
    crumbs.setAttribute("aria-label", "Breadcrumb");
    crumbs.appendChild(link("/", null, "Sessions"));
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

  function render() {
    var mount = document.getElementById("pm-nav");
    if (!mount) return;
    var spec = PAGES[mount.dataset.page];
    if (!spec) return;

    auth.then(function (info) {
      mount.textContent = "";
      mount.appendChild(buildBar(spec.tab, info));
      mount.appendChild(
        spec.context === "session" ? buildSessionContext(spec) : buildFlow(spec)
      );
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", render);
  } else {
    render();
  }
})();
