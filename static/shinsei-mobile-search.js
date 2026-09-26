/**
 * shinsei-mobile-search.js
 * Remove lupas nativas e posiciona a barra Shinsei full-width
 * abaixo do row de logo no mobile (< 750px).
 *
 * Estratégia: mover fisicamente o nó .search-action--hidden-on-drawer
 * (ou .shinsei-search-predictive) para após .header__columns,
 * dentro do .header__row--top. Mais confiável que grid tricks.
 */
(function () {
  'use strict';

  var DONE_ATTR = 'data-shinsei-mobile-search';

  function isMobile() {
    return window.innerWidth < 750;
  }

  function fixMobileHeader() {
    if (!isMobile()) return;

    var header = document.getElementById('header-component');
    if (!header) return;
    if (header.getAttribute(DONE_ATTR)) return;

    var headerRow = header.querySelector('.header__row--top');
    if (!headerRow) return;

    var columns = headerRow.querySelector('.header__columns');
    if (!columns) return;

    /* ── 1. Esconde ícones nativos de busca (lupas) ───────────────────── */
    /* .search-action--hidden-on-menu = lupa nativa visível no mobile */
    header.querySelectorAll('.search-action--hidden-on-menu').forEach(function (el) {
      el.style.setProperty('display', 'none', 'important');
    });

    /* Também esconde botões de busca inline que não sejam Shinsei */
    header.querySelectorAll('a.search-action, button.search-action').forEach(function (el) {
      if (!el.closest('.shinsei-search-predictive')) {
        el.style.setProperty('display', 'none', 'important');
      }
    });

    /* ── 2. Encontra a barra Shinsei ──────────────────────────────────── */
    var shinseiSearch = header.querySelector('.shinsei-search-predictive');
    if (!shinseiSearch) return;

    /* Sobe até o wrapper search-action (pode ter classe search-action--hidden-on-drawer) */
    var wrapper =
      shinseiSearch.closest('.search-action--hidden-on-drawer') ||
      shinseiSearch.closest('[class*="search-action"]') ||
      shinseiSearch;

    /* ── 3. Cria linha de busca abaixo do .header__columns ────────────── */
    var searchRow = document.getElementById('shinsei-mobile-search-row');
    if (!searchRow) {
      searchRow = document.createElement('div');
      searchRow.id = 'shinsei-mobile-search-row';
      searchRow.style.cssText =
        'width:100%;padding:0 12px 10px;box-sizing:border-box;display:block;';
    }

    /* Move o wrapper para a linha de busca */
    searchRow.appendChild(wrapper);

    /* Garante visibilidade */
    wrapper.style.setProperty('display', 'block', 'important');
    wrapper.style.setProperty('width', '100%', 'important');
    wrapper.style.setProperty('max-width', '100%', 'important');
    wrapper.style.setProperty('box-sizing', 'border-box', 'important');

    /* Ajusta componente preditivo e input */
    shinseiSearch.style.setProperty('width', '100%', 'important');
    shinseiSearch.style.setProperty('max-width', '100%', 'important');
    ['form', 'input', '.predictive-search-form__header', '.shinsei-search-inner'].forEach(function (sel) {
      var el = shinseiSearch.querySelector(sel);
      if (el) {
        el.style.setProperty('width', '100%', 'important');
        el.style.setProperty('max-width', '100%', 'important');
        el.style.setProperty('box-sizing', 'border-box', 'important');
      }
    });

    /* ── 4. Insere após .header__columns ──────────────────────────────── */
    columns.insertAdjacentElement('afterend', searchRow);

    header.setAttribute(DONE_ATTR, '1');
  }

  /* Executa assim que o DOM estiver pronto */
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      fixMobileHeader();
      setTimeout(fixMobileHeader, 400);
    });
  } else {
    fixMobileHeader();
    setTimeout(fixMobileHeader, 400);
  }
})();
