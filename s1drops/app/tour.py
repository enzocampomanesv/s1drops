"""First-visit walkthrough: dims the page and lights up one part at a time.

The tour runs in the browser (`Tour`): it finds each step's target with a CSS
selector, so the Python side only marks parts of the page with `s1-tour-*`
classes and lists the steps. It starts by itself on a browser's first visit
(remembered in localStorage) and again whenever `run` changes (the Tutorial
button). The page stays clickable throughout, so a step can ask the user to try
something and move on when they do (`advance_on`: a selector whose click counts).
A step whose target isn't on screen shows its card in the middle instead.
"""
from __future__ import annotations

import ipyvuetify as vy
import traitlets


def steps_for(user) -> list[dict]:
    """The steps, in the order people should work. Plain text only."""
    if user is not None and user.approved:
        validate = ("Record what you think happened here: demolition, another kind of "
                    "change, or no real change, with the evidence you used.")
    else:
        validate = ("Partners and community members sign in to record what happened "
                    "at each change. Ask the team for an account if you'd like to help.")
    return [
        {"title": "Welcome",
         "body": "This tool finds places where the radar signal from the Sentinel-1 "
                 "satellites dropped sharply and stayed low, often a sign that "
                 "buildings were removed. This short tour shows you how to explore "
                 "them. It takes about a minute."},
        {"target": ".s1-tour-area", "title": "Choose an area",
         "body": "Start by picking the city or area you want to explore."},
        {"target": ".s1-tour-cand", "title": "Pick one of the strongest changes",
         "body": "These are the places with the biggest lasting drops, strongest first. "
                 "Try it now: click a row.",
         "advance_on": ".s1-tour-cand tbody tr"},
        {"target": ".s1-map", "title": "The map",
         "body": "Coloured cells are drops in the radar signal; darker red means a "
                 "bigger drop. The place you picked is outlined. You can also click "
                 "any spot on the map to check it."},
        {"target": ".s1-tour-chart", "title": "Radar signal over time",
         "body": "How the signal changed at this place. A lasting fall below the usual "
                 "band is the change; the red dotted line is the drop shown in the "
                 "list. Hover any (i) for more explanation."},
        {"target": ".s1-tour-photos", "title": "Compare satellite photos",
         "body": "Get photos from just before and just after the change to see what "
                 "happened on the ground. Cloudy dates are skipped automatically."},
        {"target": ".s1-tour-validate", "title": "Validation", "body": validate},
        {"target": ".v-app-bar__nav-icon", "title": "More settings",
         "body": "This menu holds the period analysed, map layers, how big a change "
                 "must be to count, and the land-use filter."},
        {"target": ".s1-tour-help", "title": "That's it",
         "body": "You can run this tour again any time from here."},
    ]


_TOUR_JS = r"""
<template><span style="display: none"></span></template>
<script>
module.exports = {
  watch: {
    run() { window.__s1Tour.start(this.steps); },
  },
  mounted() {
    const KEY = 's1drops-tour-done';
    if (!window.__s1Tour) window.__s1Tour = makeTour(KEY);
    if (window.__s1TourAuto) return;   // auto-start once per page load
    window.__s1TourAuto = true;
    let seen = true;
    try { seen = !!localStorage.getItem(KEY); } catch (e) { /* blocked: don't nag */ }
    if (seen) return;
    // Wait for the page to finish drawing (the area picker is a late arrival).
    const steps = this.steps;
    let tries = 0;
    const wait = setInterval(() => {
      if (document.querySelector('.s1-tour-area') || ++tries > 60) {
        clearInterval(wait);
        setTimeout(() => window.__s1Tour.start(steps), 600);
      }
    }, 500);

    function makeTour(key) {
      const T = { i: -1, steps: [], timer: 0, spot: null, card: null };
      const el = (tag, css, text) => {
        const e = document.createElement(tag);
        if (css) e.style.cssText = css;
        if (text) e.textContent = text;
        return e;
      };
      const dark = () => !!document.querySelector('.v-application.theme--dark');
      const visible = (e) => {
        if (!e) return false;
        const r = e.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      };
      const target = () => {
        const s = T.steps[T.i];
        const e = s && s.target ? document.querySelector(s.target) : null;
        return visible(e) ? e : null;
      };

      T.start = (steps) => {
        T.steps = steps || [];
        if (!T.steps.length) return;
        if (!T.spot) {
          T.spot = el('div', 'position:fixed;z-index:3000;pointer-events:none;border-radius:6px;' +
            'box-shadow:0 0 0 9999px rgba(0,0,0,.55);outline:2px solid #2196f3;' +
            'transition:all .25s ease');
          T.card = el('div', 'position:fixed;z-index:3001;width:320px;max-width:calc(100vw - 32px);' +
            'border-radius:8px;padding:16px;box-shadow:0 8px 28px rgba(0,0,0,.4);' +
            'font:14px/1.5 Roboto,sans-serif');
          document.body.append(T.spot, T.card);
          document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && T.i >= 0) T.end(); });
          // A click on the step's "try it" element moves the tour on.
          document.addEventListener('click', (e) => {
            const s = T.steps[T.i];
            if (s && s.advance_on && e.target.closest && e.target.closest(s.advance_on)) {
              const at = T.i;
              setTimeout(() => { if (T.i === at) T.go(at + 1); }, 900);
            }
          }, true);
        }
        T.spot.style.display = T.card.style.display = '';
        clearInterval(T.timer);
        T.timer = setInterval(T.place, 250);   // follow layout changes and scrolling
        T.go(0);
      };

      T.end = () => {
        T.i = -1;
        clearInterval(T.timer);
        if (T.spot) T.spot.style.display = T.card.style.display = 'none';
        try { localStorage.setItem(key, '1'); } catch (e) { /* not remembered */ }
      };

      T.go = (i) => {
        if (i < 0) return;
        if (i >= T.steps.length) return T.end();
        T.i = i;
        const s = T.steps[i];
        const d = dark();
        T.card.style.background = d ? '#2b2b2b' : 'white';
        T.card.style.color = d ? '#eee' : '#222';
        T.card.replaceChildren();
        T.card.append(el('div', 'font-weight:600;font-size:16px;margin-bottom:6px', s.title));
        T.card.append(el('div', 'margin-bottom:12px', s.body));
        const row = el('div', 'display:flex;align-items:center;gap:8px');
        row.append(el('span', 'opacity:.6;font-size:12px;flex:1', (i + 1) + ' of ' + T.steps.length));
        const btn = (label, primary, fn) => {
          const b = el('button', 'border:0;border-radius:4px;padding:6px 12px;cursor:pointer;' +
            'font:500 13px Roboto,sans-serif;' + (primary
              ? 'background:#2196f3;color:white'
              : 'background:transparent;color:inherit;opacity:.8'), label);
          b.onclick = fn;
          return b;
        };
        if (i < T.steps.length - 1) row.append(btn('Skip tour', false, T.end));
        if (i > 0) row.append(btn('Back', false, () => T.go(i - 1)));
        row.append(btn(i === T.steps.length - 1 ? 'Done' : 'Next', true, () => T.go(i + 1)));
        T.card.append(row);
        const t = target();
        if (t) t.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        T.place();
      };

      T.place = () => {
        if (T.i < 0) return;
        const vw = window.innerWidth, vh = window.innerHeight, pad = 6, gap = 14;
        const cw = T.card.offsetWidth, ch = T.card.offsetHeight;
        const t = target();
        if (!t) {   // nothing to point at: no spotlight, card in the middle
          Object.assign(T.spot.style, { left: vw / 2 + 'px', top: vh / 2 + 'px', width: '0px', height: '0px', outline: 'none' });
          Object.assign(T.card.style, { left: (vw - cw) / 2 + 'px', top: (vh - ch) / 2 + 'px' });
          return;
        }
        const r = t.getBoundingClientRect();
        const L = Math.max(r.left - pad, 0), Tp = Math.max(r.top - pad, 0);
        const R = Math.min(r.right + pad, vw), B = Math.min(r.bottom + pad, vh);
        Object.assign(T.spot.style, { left: L + 'px', top: Tp + 'px', width: Math.max(R - L, 0) + 'px',
                                      height: Math.max(B - Tp, 0) + 'px', outline: '2px solid #2196f3' });
        const clampY = (y) => Math.min(Math.max(y, 8), vh - ch - 8);
        const clampX = (x) => Math.min(Math.max(x, 8), vw - cw - 8);
        let x, y;
        if (R + gap + cw < vw) { x = R + gap; y = clampY(Tp); }            // right
        else if (L - gap - cw > 0) { x = L - gap - cw; y = clampY(Tp); }   // left
        else if (B + gap + ch < vh) { x = clampX(L); y = B + gap; }        // below
        else if (Tp - gap - ch > 0) { x = clampX(L); y = Tp - gap - ch; }  // above
        else { x = clampX(R - cw - 16); y = vh - ch - 16; }               // inside, bottom
        Object.assign(T.card.style, { left: x + 'px', top: y + 'px' });
      };
      return T;
    }
  },
};
</script>
"""


class Tour(vy.VuetifyTemplate):
    """Invisible widget whose browser-side script runs the walkthrough."""
    template = traitlets.Unicode(_TOUR_JS).tag(sync=True)
    steps = traitlets.List([]).tag(sync=True)
    run = traitlets.Int(0).tag(sync=True)
