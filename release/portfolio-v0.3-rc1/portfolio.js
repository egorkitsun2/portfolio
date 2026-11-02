(() => {
  const menu = document.getElementById('menu');
  const nav = document.getElementById('nav');
  const setMenu = open => {
    nav.classList.toggle('show', open);
    menu.setAttribute('aria-expanded', String(open));
    menu.setAttribute('aria-label', open ? 'Закрыть меню' : 'Открыть меню');
    if (open) nav.querySelector('a').focus();
  };
  menu.addEventListener('click', () => setMenu(menu.getAttribute('aria-expanded') !== 'true'));
  nav.querySelectorAll('a').forEach(link => link.addEventListener('click', () => setMenu(false)));
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && menu.getAttribute('aria-expanded') === 'true') {setMenu(false); menu.focus();}
  });
  document.addEventListener('click', e => {if (!e.target.closest('.top')) setMenu(false);});
  nav.addEventListener('focusout', () => requestAnimationFrame(() => {
    if (!nav.contains(document.activeElement) && document.activeElement !== menu) setMenu(false);
  }));
  matchMedia('(min-width:761px)').addEventListener('change', () => setMenu(false));
  const motion = matchMedia('(prefers-reduced-motion: reduce)');
  const revealElements = [...document.querySelectorAll('.reveal')];
  if ('IntersectionObserver' in window && !motion.matches) {
    const observer = new IntersectionObserver(entries => {
      entries.forEach(entry => {if (entry.isIntersecting) {entry.target.classList.add('visible'); observer.unobserve(entry.target);}});
    }, {threshold:.08});
    revealElements.forEach(el => {el.classList.add('will-reveal'); observer.observe(el);});
    document.addEventListener('focusin', e => e.target.closest('.reveal')?.classList.add('visible'));
    motion.addEventListener('change', e => {if (e.matches) {observer.disconnect(); revealElements.forEach(el => el.classList.add('visible'));}});
  }
  if ('IntersectionObserver' in window) {
    const spy = new IntersectionObserver(entries => entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      nav.querySelectorAll('a').forEach(link => {
        const active = link.hash === '#' + entry.target.id;
        link.classList.toggle('active', active);
        if (active) link.setAttribute('aria-current','location'); else link.removeAttribute('aria-current');
      });
    }), {rootMargin:'-15% 0px -60% 0px'});
    document.querySelectorAll('main > section[id]').forEach(el => spy.observe(el));
  }
  const dialog = document.getElementById('project-quick-dialog');
  PortfolioUI.register(dialog);
  function render(key) {
    const p = PORTFOLIO_PROJECTS[key];
    if (!p) return;
    document.getElementById('project-quick-type').textContent = p.type;
    document.getElementById('project-quick-title').textContent = p.name;
    document.getElementById('project-quick-summary').textContent = p.summary;
    document.getElementById('project-quick-features').replaceChildren(...p.features.map(text => {
      const li = document.createElement('li'); li.textContent = text; return li;
    }));
    document.getElementById('project-quick-note').textContent = p.note;
    document.getElementById('project-quick-detail').href = p.details;
    PortfolioMedia.render(document.getElementById('project-quick-media'), key);
    PortfolioMedia.live(document.getElementById('project-quick-live'), key);
  }
  document.querySelectorAll('[data-project]').forEach(link => {
    link.setAttribute('aria-haspopup','dialog');
    link.setAttribute('aria-controls',dialog.id);
    link.addEventListener('click', e => {
      if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
      if (!PORTFOLIO_PROJECTS[link.dataset.project]) return;
      e.preventDefault();
      render(link.dataset.project);
      PortfolioUI.show(dialog, link, link.dataset.project);
    });
  });
  document.addEventListener('portfolio:restore-project', e => render(e.detail));
  const saved = history.state?.portfolioModal;
  if (saved?.id === dialog.id && PORTFOLIO_PROJECTS[saved.project]) {
    render(saved.project);
    PortfolioUI.show(dialog, document.querySelector('[data-project="'+saved.project+'"]'), saved.project, true);
  }
  if (saved?.id === 'contact-dialog') PortfolioUI.show(document.getElementById('contact-dialog'), null, null, true);
  dialog.querySelectorAll('button').forEach(button => button.addEventListener('click', () => PortfolioUI.close(dialog)));
})();
