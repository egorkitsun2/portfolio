(() => {
  const base = new URL('.', document.currentScript.src);
  const asset = value => {
    if (typeof value !== 'string' || !value.startsWith('assets/projects/') || value.includes('..')) return '';
    return new URL(value, base).href;
  };
  const publicUrl = value => {
    try {
      const u = new URL(value);
      if (u.protocol !== 'https:' || u.username || u.password || /^(localhost|127\.|0\.|\[::1\])/.test(u.hostname)) return '';
      return u.href;
    } catch { return ''; }
  };
  function live(container, key) {
    const p = PORTFOLIO_PROJECTS[key];
    container.replaceChildren();
    if (['ros','archive'].includes(key)) return;
    const url = publicUrl(p.liveUrl);
    const element = document.createElement(url ? 'a' : 'span');
    element.className = url ? 'project-live' : 'live-status';
    element.textContent = url ? 'Посмотреть сайт ↗' : 'Публичная версия пока не указана';
    if (url) {
      element.href=url; element.target='_blank'; element.rel='noopener noreferrer';
      element.setAttribute('aria-label', 'Посмотреть сайт «' + p.name + '» (новая вкладка)');
    }
    container.append(element);
  }
  function render(container, key) {
    const p = PORTFOLIO_PROJECTS[key];
    container.replaceChildren();
    container.classList.add('media-gallery');
    for (const item of p.screenshots) {
      const src = asset(item.src);
      if (!src) continue;
      const figure = document.createElement('figure');
      const img = document.createElement('img');
      img.src=src; img.alt=item.alt; img.width=item.width || 1440; img.height=item.height || 1000; img.loading='lazy'; img.decoding='async';
      const caption = document.createElement('figcaption');
      caption.append(document.createTextNode(item.caption + ' · '));
      const full = document.createElement('a');
      full.href=src; full.target='_blank'; full.rel='noopener noreferrer'; full.textContent='Открыть скриншот';
      full.setAttribute('aria-label','Открыть скриншот «'+p.name+'» в новой вкладке');
      caption.append(full); figure.append(img,caption); container.append(figure);
    }
    if (!p.screenshots.length) {
      const pending = document.createElement('p');
      pending.className='media-pending';
      pending.textContent='Скриншоты работающего интерфейса пока не предоставлены.';
      container.append(pending);
    }
    const video = p.video;
    if (video && asset(video.src)) {
      const player = document.createElement('video');
      player.controls=true; player.preload='none'; player.playsInline=true;
      player.src=asset(video.src); player.setAttribute('aria-label','Видеодемонстрация: '+p.name);
      if (asset(video.poster)) player.poster=asset(video.poster);
      if (asset(video.captions)) {
        const track=document.createElement('track');
        track.kind='captions'; track.srclang='ru'; track.label='Русские субтитры'; track.src=asset(video.captions); track.default=true; player.append(track);
      }
      container.append(player);
      if (video.description) {const text=document.createElement('p');text.className='media-pending';text.textContent=video.description;container.append(text);}
    } else if (['ros','archive'].includes(key)) {
      const pending=document.createElement('p');pending.className='media-pending';
      pending.textContent='Видеодемонстрация будет доступна после предоставления записи владельцем.';
      container.append(pending);
    }
  }
  window.PortfolioMedia={render,live};
  function init() {
    document.querySelectorAll('.other-row[data-project]').forEach(row => {
      const container=document.createElement('div');container.className='other-live';
      container.dataset.liveProject=row.dataset.project;row.after(container);
    });
    document.querySelectorAll('[data-live-project]').forEach(el => live(el,el.dataset.liveProject));
    const key=document.body.dataset.projectCase;
    if (key) {
      render(document.getElementById('case-media'), key);
      document.querySelector('[data-return-project]')?.addEventListener('click', e => {
        if (e.button!==0||e.ctrlKey||e.metaKey||e.shiftKey||e.altKey) return;
        let ref;
        try {ref=new URL(document.referrer);} catch {return;}
        if (ref.origin===location.origin && /\/(index\.html)?$/.test(ref.pathname) && history.length>1) {e.preventDefault();history.back();}
      });
    }
  }
  if (document.readyState==='loading') document.addEventListener('DOMContentLoaded',init); else init();
})();