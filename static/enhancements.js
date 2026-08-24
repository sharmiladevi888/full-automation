;(function(){
  'use strict';

  /* === CURSOR GLOW === */
  const glow = document.createElement('div');
  glow.className = 'cursor-glow';
  document.body.appendChild(glow);
  let glowTimer;
  document.addEventListener('mousemove', e => {
    glow.style.left = e.clientX + 'px';
    glow.style.top = e.clientY + 'px';
    glow.classList.add('active');
    clearTimeout(glowTimer);
    glowTimer = setTimeout(() => glow.classList.remove('active'), 3000);
  }, {passive: true});

  /* === BUTTON RIPPLE === */
  document.addEventListener('mousedown', e => {
    const btn = e.target.closest('.btn, .ghost-btn');
    if (!btn) return;
    const r = btn.getBoundingClientRect();
    const sz = Math.max(r.width, r.height) * 2;
    const rip = document.createElement('span');
    rip.style.cssText = `
      position:absolute; border-radius:50%; pointer-events:none;
      width:${sz}px; height:${sz}px;
      left:${e.clientX - r.left - sz/2}px;
      top:${e.clientY - r.top - sz/2}px;
      background:rgba(255,255,255,0.2);
      animation:rippleOut 0.6s ease-out forwards;
    `;
    btn.style.position = 'relative';
    btn.style.overflow = 'hidden';
    btn.appendChild(rip);
    setTimeout(() => rip.remove(), 600);
  });

  /* === SCROLL REVEAL === */
  const revealObs = new IntersectionObserver(entries => {
    entries.forEach(ent => {
      if (ent.isIntersecting) {
        ent.target.style.animation = 'fadeUp 0.5s cubic-bezier(0.25,0.46,0.45,0.94) forwards';
        revealObs.unobserve(ent.target);
      }
    });
  }, {threshold: 0.08, rootMargin: '0px 0px -40px 0px'});

  function initReveals() {
    document.querySelectorAll('.card, .shot, .scenelet, .audio-card, .frame').forEach(el => {
      if (!el.dataset.revealed) {
        el.dataset.revealed = '1';
        el.style.opacity = '0';
        revealObs.observe(el);
      }
    });
  }
  new MutationObserver(() => requestAnimationFrame(initReveals))
    .observe(document.body, {childList: true, subtree: true});

  /* === HEADER SHADOW === */
  const hdr = document.querySelector('header');
  if (hdr) {
    let ticking = false;
    window.addEventListener('scroll', () => {
      if (!ticking) {
        requestAnimationFrame(() => {
          hdr.style.boxShadow = window.scrollY > 10
            ? '0 4px 40px -10px rgba(0,0,0,0.6), 0 1px 0 rgba(249,115,22,0.05)' : '';
          ticking = false;
        });
        ticking = true;
      }
    }, {passive: true});
  }

  /* === KEYBOARD SHORTCUTS === */
  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key >= '1' && e.key <= '9') {
      e.preventDefault();
      const tabs = document.querySelectorAll('.tab');
      const i = parseInt(e.key) - 1;
      if (tabs[i]) tabs[i].click();
    }
  });

  /* === LOADING BAR === */
  const loadBar = document.createElement('div');
  loadBar.style.cssText = `
    position:fixed; top:0; left:0; height:2px; z-index:99999;
    background:linear-gradient(90deg, var(--amber,#f97316), var(--violet,#8b5cf6));
    width:0; transition: width 0.3s, opacity 0.3s; opacity:0;
    box-shadow: 0 0 10px rgba(249,115,22,0.5);
  `;
  document.body.appendChild(loadBar);
  let reqCount = 0;
  const _fetch = window.fetch;
  window.fetch = function(...args) {
    const url = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';
    if (url.startsWith('/api/')) {
      reqCount++;
      loadBar.style.opacity = '1';
      loadBar.style.width = '70%';
    }
    return _fetch.apply(this, args).finally(() => {
      if (url.startsWith('/api/')) {
        reqCount = Math.max(0, reqCount - 1);
        if (reqCount === 0) {
          loadBar.style.width = '100%';
          setTimeout(() => { loadBar.style.opacity = '0'; loadBar.style.width = '0'; }, 300);
        }
      }
    });
  };

  /* === CARD TILT === */
  document.addEventListener('mousemove', e => {
    const card = e.target.closest('.card, .shot');
    if (!card) return;
    const r = card.getBoundingClientRect();
    const x = (e.clientX - r.left) / r.width - 0.5;
    const y = (e.clientY - r.top) / r.height - 0.5;
    card.style.transform = `perspective(800px) rotateY(${x*4}deg) rotateX(${-y*4}deg) translateY(-6px) scale(1.01)`;
  }, {passive: true});
  document.addEventListener('mouseleave', e => {
    const card = e.target.closest('.card, .shot');
    if (card) card.style.transform = '';
  }, true);

  document.addEventListener('DOMContentLoaded', initReveals);
  setTimeout(initReveals, 500);
  console.log('%c\u2728 Continuity Studio %cPRO','color:#f97316;font-weight:900;font-size:16px','color:#8b5cf6;font-weight:900;font-size:16px');
})();
