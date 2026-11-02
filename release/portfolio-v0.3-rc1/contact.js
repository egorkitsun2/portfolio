/* Owner-provided public contacts; web destinations checked on 2026-11-02. */
const CONTACT_LINKS = {
  whatsapp: "https://wa.me/77779409625",
  telegram: "https://t.me/GLhost27",
  instagram: "https://www.instagram.com/egorkitsun2/",
  phone: "tel:+77013061220",
  github: "https://github.com/egorkitsun2"
};
(() => {
  const registered = new Map();
  let closing = false;
  const syncLock = () => document.body.classList.toggle('modal-open', !!document.querySelector('dialog[open]'));
  function show(dialog, trigger, project = null, restore = false) {
    for (const other of registered.keys()) if (other !== dialog && other.open) other.close();
    if (trigger) dialog._returnFocus = trigger;
    if (!restore) history.pushState({...history.state, portfolioModal: {id: dialog.id, project}}, '');
    if (!dialog.open) dialog.showModal();
    dialog.scrollTop = 0;
    syncLock();
    dialog.querySelector('button')?.focus({preventScroll: true});
  }
  function close(dialog) {
    if (closing || !dialog.open) return;
    if (history.state?.portfolioModal?.id === dialog.id) {
      closing = true;
      history.back();
    } else dialog.close();
  }
  function register(dialog) {
    registered.set(dialog, true);
    let backdropStart = false;
    const outside = e => {
      const r = dialog.getBoundingClientRect();
      return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
    };
    dialog.addEventListener('pointerdown', e => { backdropStart = e.target === dialog && outside(e); });
    dialog.addEventListener('click', e => { if (backdropStart && e.target === dialog && outside(e)) close(dialog); backdropStart = false; });
    dialog.addEventListener('cancel', e => { e.preventDefault(); close(dialog); });
    dialog.addEventListener('keydown', e => {
      if (e.key !== 'Tab') return;
      const items = [...dialog.querySelectorAll('a[href],button:not([disabled]),video[controls],[tabindex="0"]')].filter(el => el.getClientRects().length);
      const first = items[0], last = items.at(-1);
      if (!first) {e.preventDefault(); dialog.focus(); return;}
      if (e.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) {e.preventDefault();last.focus();}
      else if (!e.shiftKey && (document.activeElement === last || !dialog.contains(document.activeElement))) {e.preventDefault();first.focus();}
    });
    dialog.addEventListener('close', () => {
      dialog.querySelectorAll('video').forEach(v => v.pause());
      syncLock();
      if (!document.querySelector('dialog[open]') && dialog._returnFocus?.isConnected) {
        const target = dialog._returnFocus.getClientRects().length ? dialog._returnFocus : document.getElementById('menu');
        target?.focus({preventScroll:true});
      }
    });
  }
  window.addEventListener('popstate', () => {
    closing = false;
    const state = history.state?.portfolioModal;
    for (const dialog of registered.keys()) if (dialog.id !== state?.id && dialog.open) dialog.close();
    if (state) {
      const dialog = document.getElementById(state.id);
      if (dialog && registered.has(dialog)) {
        if (state.project) document.dispatchEvent(new CustomEvent('portfolio:restore-project', {detail:state.project}));
        show(dialog, null, state.project, true);
      }
    }
    syncLock();
  });
  window.PortfolioUI = {show, close, register};
  const safeUrl = (value, key) => {
    if (key === "phone") return /^tel:\+[1-9]\d{7,14}$/.test(value) ? value : "";
    try { const url = new URL(value); return url.protocol === 'https:' && !url.username && !url.password ? url.href : ''; }
    catch { return ''; }
  };
  const labels = [
    {key:'whatsapp',label:'WhatsApp',icon:'◉',note:'Перейти в чат',primary:true},
    {key:'telegram',label:'Telegram',icon:'➤',note:'Написать сообщение'},
    {key:'instagram',label:'Instagram',icon:'◎',note:'Перейти в профиль'},
    {key:'phone',label:'Позвонить',icon:'☎',note:'+7 (701) 306-12-20'},
    {key:'github',label:'GitHub',icon:'⌘',note:'Посмотреть профиль разработчика'}
  ];
  const dialog = document.createElement('dialog');
  dialog.id = 'contact-dialog';
  dialog.className = 'contact-dialog';
  dialog.setAttribute('aria-labelledby','contact-dialog-title');
  dialog.innerHTML = `<div class="contact-inner"><div class="contact-top"><span>Обсудим Ваш проект</span><button class="contact-close" type="button" aria-label="Закрыть контакты">×</button></div><h2 id="contact-dialog-title">Как Вам удобнее связаться?</h2><p class="contact-desc">Доступные способы связи.</p><div class="contact-channel-list"></div><p class="contact-help"></p></div>`;
  document.body.append(dialog);
  register(dialog);
  const list = dialog.querySelector('.contact-channel-list');
  let active = 0;
  for (const item of labels) {
    const url = safeUrl(CONTACT_LINKS[item.key], item.key);
    const channel = document.createElement(url ? 'a' : 'div');
    channel.className = 'contact-channel ' + (item.primary ? 'primary ' : '') + (url ? 'is-active' : 'is-unavailable');
    channel.innerHTML = `<span class="chan-icon" aria-hidden="true">${item.icon}</span><span class="channel-text">${item.label}<small>${url ? item.note + (item.key === 'phone' ? '' : ' · новая вкладка') : 'Контакт пока не предоставлен'}</small></span><span class="chan-arrow" aria-hidden="true">${url ? (item.key === 'phone' ? '→' : '↗') : '—'}</span>`;
    if (url) {
      channel.href=url;
      if (item.key !== 'phone') {channel.target='_blank'; channel.rel='noopener noreferrer';}
      active++;
    }
    list.append(channel);
  }
  dialog.querySelector('.contact-help').textContent = active ? 'Мессенджеры и профили открываются в новой вкладке.' : 'Контакты будут доступны после подтверждения владельцем.';
  dialog.querySelector('button').addEventListener('click', () => close(dialog));
  document.querySelectorAll('[data-contact]').forEach(link => {
    link.setAttribute('aria-haspopup','dialog');
    link.setAttribute('aria-controls',dialog.id);
    link.addEventListener('click', e => {
      if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
      e.preventDefault();
      show(dialog, link);
    });
  });
  if (history.state?.portfolioModal?.id === dialog.id) show(dialog, null, null, true);
  else if (new URLSearchParams(location.search).has('contact')) show(dialog, null);
})();
