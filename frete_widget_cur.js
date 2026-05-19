/**
 * frete-widget.js — Widget de progresso de frete para Shinsei Market
 *
 * v3 — click handling robusto:
 *  - onclick direto no botao (via atributo HTML) -> window.ShinseiFreteCalc
 *  - document.addEventListener capture:true (sobrevive a stopPropagation)
 *  - MutationObserver adiciona listener direto no botao quando aparece no DOM
 *  - Eventos Shopify / Horizon para re-render do carrinho
 */

(function () {
  'use strict';

  var CONFIG = window.ShinseiFreteConfig || {};
  var API_BASE          = (CONFIG.apiBase || '').replace(/\/$/, '');
  var SUBSIDIO_POR_ITEM = CONFIG.subsidioPorItem  || 8;
  var FRETE_REAL_DEFAULT = CONFIG.freteRealDefault || 18;
  var CEP_STORAGE_KEY   = 'shinsei_frete_cep';

  var state = {
    cep:      localStorage.getItem(CEP_STORAGE_KEY) || '',
    qty:      1,
    peso:     0.3,
    valor:    0,
    loading:  false,
    resultado: null,
    _loadingTimer: null,
  };

  // CEP helpers
  function formatCep(value) {
    var digits = value.replace(/\D/g, '').slice(0, 8);
    return digits.length > 5 ? digits.slice(0, 5) + '-' + digits.slice(5) : digits;
  }
  function rawCep(value) {
    return (value || '').replace(/\D/g, '');
  }

  // Cart data
  function fetchCart(callback) {
    fetch('/cart.js')
      .then(function (r) { return r.json(); })
      .then(function (cart) {
        var qty = 0, peso = 0, valor = 0;
        var items = cart.items || [];
        for (var i = 0; i < items.length; i++) {
          var item = items[i];
          qty   += item.quantity || 1;
          peso  += ((item.grams || 300) * (item.quantity || 1)) / 1000;
          valor += ((item.price || 0) / 100) * (item.quantity || 1);
        }
        callback(null, { qty: Math.max(1, qty), peso: Math.max(0.1, peso), valor: valor });
      })
      .catch(function (err) { callback(err, null); });
  }

  // API calls
  function fetchProgresso(qty, callback) {
    var url = API_BASE + '/frete/progresso?qty=' + qty + '&frete_real=' + FRETE_REAL_DEFAULT;
    fetch(url)
      .then(function (r) { return r.json(); })
      .then(function (data) { callback(null, data); })
      .catch(function (err) { callback(err, null); });
  }

  function fetchCalculo(cep, qty, peso, valor, callback) {
    var url = API_BASE + '/frete/calcular?cep=' + cep
      + '&qty=' + qty
      + '&peso=' + peso.toFixed(3)
      + '&valor=' + valor.toFixed(2);
    fetch(url)
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (data) { callback(null, data); })
      .catch(function (err) { callback(err, null); });
  }

  // UI helpers
  function $id(id) { return document.getElementById(id); }

  function updateUI(data) {
    var resultado = $id('shinsei-frete-resultado');
    var barraFill = $id('shinsei-barra-fill');
    var mensagem  = $id('shinsei-frete-mensagem');
    var opcoes    = $id('shinsei-frete-opcoes');
    var erro      = $id('shinsei-frete-erro');
    if (!resultado) return;
    if (erro) erro.style.display = 'none';
    resultado.style.display = 'block';

    if (data.options !== undefined) {
      var cheapestReal = data.options.length > 0
        ? Math.min.apply(null, data.options.map(function (o) { return o.price_real; }))
        : FRETE_REAL_DEFAULT;
      var subsidio = data.subsidy_total || (SUBSIDIO_POR_ITEM * data.qty_items);
      var progPct  = cheapestReal > 0 ? Math.min(100, Math.round((subsidio / cheapestReal) * 100)) : 100;
      if (barraFill) barraFill.style.width = progPct + '%';
      if (data.is_free) {
        if (mensagem) mensagem.innerHTML = '<span class="frete-gratis">Parabens! Voce ganhou frete gratis!</span>';
        if (barraFill) barraFill.classList.add('frete-barra-completa');
      } else {
        if (barraFill) barraFill.classList.remove('frete-barra-completa');
        var faltam = data.items_for_free_shipping || 0;
        if (mensagem) {
          if (faltam === 1) mensagem.textContent = 'Adicione 1 item para frete gratis!';
          else if (faltam > 1) mensagem.textContent = 'Adicione ' + faltam + ' itens para frete gratis!';
          else mensagem.textContent = 'Quase la! Frete com desconto aplicado.';
        }
      }
      if (opcoes) {
        if (data.options && data.options.length > 0) {
          var html = '<ul class="frete-lista-opcoes">';
          data.options.forEach(function (opt) {
            var priceStr = opt.is_free
              ? '<span class="frete-gratis-tag">GRATIS</span>'
              : 'R$&nbsp;' + opt.price_final.toFixed(2).replace('.', ',');
            html += '<li class="frete-opcao' + (opt.is_free ? ' frete-opcao-gratis' : '') + '">';
            html += '<span class="frete-opcao-nome">' + opt.name + '</span>';
            html += '<span class="frete-opcao-prazo">' + opt.delivery_days + '&nbsp;dias uteis</span>';
            html += '<span class="frete-opcao-preco">' + priceStr + '</span>';
            html += '</li>';
          });
          html += '</ul>';
          if (data.city) html += '<p class="frete-cidade">Entrega em: ' + data.city + ' - ' + data.state + '</p>';
          opcoes.innerHTML = html;
        } else {
          opcoes.innerHTML = '';
        }
      }
    } else {
      var pct = data.progresso_pct || 0;
      if (barraFill) barraFill.style.width = pct + '%';
      if (data.eh_gratis) {
        if (mensagem) mensagem.innerHTML = '<span class="frete-gratis">Parabens! Voce ganhou frete gratis!</span>';
        if (barraFill) barraFill.classList.add('frete-barra-completa');
      } else {
        if (barraFill) barraFill.classList.remove('frete-barra-completa');
        if (mensagem) mensagem.textContent = data.mensagem || 'Calcule o frete informando seu CEP.';
      }
      if (opcoes) opcoes.innerHTML = '';
    }
  }

  function showError(msg) {
    var erro = $id('shinsei-frete-erro');
    var resultado = $id('shinsei-frete-resultado');
    if (erro) { erro.textContent = msg; erro.style.display = 'block'; }
    if (resultado) resultado.style.display = 'none';
  }

  function setLoading(active) {
    var btn = $id('shinsei-cep-btn');
    if (btn) { btn.disabled = active; btn.textContent = active ? '...' : 'Calcular'; }
  }

  function fillCepInput() {
    var input = $id('shinsei-cep-input');
    if (input && state.cep) input.value = formatCep(state.cep);
  }

  // Loading guard com timeout anti-stuck (10s)
  function setLoadingTrue() {
    if (state._loadingTimer) clearTimeout(state._loadingTimer);
    state.loading = true;
    state._loadingTimer = setTimeout(function () {
      state.loading = false;
      state._loadingTimer = null;
      setLoading(false);
    }, 10000);
  }
  function setLoadingFalse() {
    if (state._loadingTimer) clearTimeout(state._loadingTimer);
    state._loadingTimer = null;
    state.loading = false;
    setLoading(false);
  }

  // Fallback local — estimativa sem API (backend temporariamente indisponivel)
  function showFallbackLocal(qty) {
    var subsidio = SUBSIDIO_POR_ITEM * (qty || 1);
    var frete_real = FRETE_REAL_DEFAULT;
    var frete_final = Math.max(0, frete_real - subsidio);
    var eh_gratis = frete_final === 0;

    var resultado = $id('shinsei-frete-resultado');
    var barraFill = $id('shinsei-barra-fill');
    var mensagem  = $id('shinsei-frete-mensagem');
    var opcoes    = $id('shinsei-frete-opcoes');
    var erro      = $id('shinsei-frete-erro');

    if (erro) erro.style.display = 'none';
    if (resultado) resultado.style.display = 'block';

    var pct = frete_real > 0 ? Math.min(100, Math.round(subsidio / frete_real * 100)) : 100;
    if (barraFill) {
      barraFill.style.width = pct + '%';
      if (eh_gratis) barraFill.classList.add('frete-barra-completa');
      else barraFill.classList.remove('frete-barra-completa');
    }

    if (mensagem) {
      if (eh_gratis) {
        mensagem.innerHTML = '<span class="frete-gratis">Parabens! Voce ganhou frete gratis!</span>';
      } else {
        var faltam = Math.ceil((frete_real - subsidio) / SUBSIDIO_POR_ITEM);
        mensagem.textContent = faltam > 0
          ? 'Adicione ' + faltam + ' item' + (faltam > 1 ? 's' : '') + ' para frete gratis!'
          : 'Quase la! Frete com desconto aplicado.';
      }
    }

    if (opcoes) {
      var precoStr = eh_gratis
        ? '<span class="frete-gratis-tag">GRATIS</span>'
        : 'R$ ' + frete_final.toFixed(2).replace('.', ',');
      opcoes.innerHTML =
        '<ul class="frete-lista-opcoes">' +
        '<li class="frete-opcao' + (eh_gratis ? ' frete-opcao-gratis' : '') + '">' +
        '<span class="frete-opcao-nome">Entrega Padrao</span>' +
        '<span class="frete-opcao-prazo">5&nbsp;dias uteis</span>' +
        '<span class="frete-opcao-preco">' + precoStr + '</span>' +
        '</li></ul>' +
        '<p class="frete-cidade" style="font-size:11px;opacity:.7">* Estimativa — insira o CEP para calcular com precisao</p>';
    }
  }

  // Trigger principal — exposto globalmente para o onclick HTML
  function triggerCalcular() {
    var input = $id('shinsei-cep-input');
    if (!input) return;
    var cep = rawCep(input.value);
    if (cep.length !== 8) { showError('CEP invalido. Digite 8 digitos.'); return; }
    state.cep = cep;
    try { localStorage.setItem(CEP_STORAGE_KEY, cep); } catch(e) {}
    update(cep);
  }

  // Expoe globalmente para o onclick no botao
  window.ShinseiFreteCalc = triggerCalcular;

  // Update principal
  function update(forceCep) {
    if (state.loading) return;
    setLoadingTrue();
    setLoading(true);
    fetchCart(function (err, cart) {
      if (err || !cart) { setLoadingFalse(); return; }
      state.qty   = cart.qty;
      state.peso  = cart.peso;
      state.valor = cart.valor;
      var cep = rawCep(forceCep || state.cep || '');
      if (cep.length === 8) {
        fetchCalculo(cep, state.qty, state.peso, state.valor, function (err2, data) {
          setLoadingFalse();
          if (err2 || !data) {
            // Fallback local: CEP invalido ou backend temporariamente fora
            var isNetworkErr = err2 && (err2.message || '').indexOf('HTTP') === -1;
            if (isNetworkErr) {
              // Backend fora — mostra estimativa local
              showFallbackLocal(state.qty);
            } else {
              showError('CEP nao encontrado. Verifique e tente novamente.');
            }
          } else {
            updateUI(data);
            state.resultado = data;
          }
        });
      } else {
        fetchProgresso(state.qty, function (err2, data) {
          setLoadingFalse();
          if (err2 || !data) return;
          updateUI(data);
          state.resultado = data;
        });
      }
    });
  }

  // Re-render callback
  var _afterRenderTimer = null;
  function onAfterRender() {
    if (_afterRenderTimer) clearTimeout(_afterRenderTimer);
    _afterRenderTimer = setTimeout(function () {
      _afterRenderTimer = null;
      var cfg = window.ShinseiFreteConfig || {};
      if (cfg.apiBase)          API_BASE           = cfg.apiBase.replace(/\/$/, '');
      if (cfg.subsidioPorItem)  SUBSIDIO_POR_ITEM  = cfg.subsidioPorItem;
      if (cfg.freteRealDefault) FRETE_REAL_DEFAULT = cfg.freteRealDefault;
      fillCepInput();
      attachBtnListener();
      if (!state.loading) update();
    }, 150);
  }

  // Listener direto no botao
  function attachBtnListener() {
    var btn = $id('shinsei-cep-btn');
    if (btn && !btn._shinseiListened) {
      btn._shinseiListened = true;
      btn.addEventListener('mousedown', function (e) {
        e.stopPropagation();
        triggerCalcular();
      });
    }
  }

  // Cart polling
  function startCartTokenPoll() {
    var _origFetch = window.fetch;
    var _lastToken = null;
    setInterval(function () {
      _origFetch.call(window, '/cart.js')
        .then(function (r) { return r.json(); })
        .then(function (cart) {
          var token = cart.token;
          if (_lastToken !== null && token !== _lastToken) setTimeout(onAfterRender, 150);
          _lastToken = token;
        })
        .catch(function () {});
    }, 1200);
  }

  // Event delegation — capture:true dispara antes de qualquer stopPropagation
  function setupDelegation() {
    document.addEventListener('click', function (e) {
      if (e.target && e.target.id === 'shinsei-cep-btn') triggerCalcular();
    }, true);

    document.addEventListener('keydown', function (e) {
      if (e.target && e.target.id === 'shinsei-cep-input' && e.key === 'Enter') {
        e.preventDefault();
        triggerCalcular();
      }
    }, true);

    document.addEventListener('input', function (e) {
      if (e.target && e.target.id === 'shinsei-cep-input') {
        e.target.value = formatCep(e.target.value);
        if (rawCep(e.target.value).length === 8) triggerCalcular();
      }
    });

    document.addEventListener('shopify:section:load', function () { setTimeout(onAfterRender, 100); });
    document.addEventListener('theme:cart:change',    function () { onAfterRender(); });
    document.addEventListener('cart:updated',         function () { onAfterRender(); });
    document.addEventListener('cart:refresh',         function () { onAfterRender(); });
  }

  // MutationObserver — detecta quando o botao aparece no DOM
  function setupMutationObserver() {
    var observer = new MutationObserver(function (mutations) {
      var hasWidget = mutations.some(function (m) {
        return Array.from(m.addedNodes).some(function (n) {
          return n.nodeType === 1 && (
            n.id === 'shinsei-frete-widget' ||
            n.id === 'shinsei-cep-btn' ||
            (n.querySelector && n.querySelector('#shinsei-cep-btn'))
          );
        });
      });
      if (hasWidget) setTimeout(function () { fillCepInput(); attachBtnListener(); }, 50);
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }

  // Bootstrap
  function init() {
    if (window.ShinseiFreteInitialized) return;
    window.ShinseiFreteInitialized = true;
    window.ShinseiFreteCalc = triggerCalcular;
    setupDelegation();
    setupMutationObserver();
    startCartTokenPoll();
    fillCepInput();
    attachBtnListener();
    setTimeout(function () { update(); }, 200);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

})();
