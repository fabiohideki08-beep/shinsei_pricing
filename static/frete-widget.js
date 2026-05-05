/**
 * frete-widget.js — Widget de progresso de frete para Shinsei Market
 *
 * v2 — compatível com AJAX section re-rendering do Horizon:
 *  - HTML fica no Liquid (servidor), não é mais injetado via JS
 *  - Todos os event listeners usam delegação no document (sobrevivem ao re-render)
 *  - shopify:section:load re-preenche o CEP e atualiza o frete após re-render
 */

(function () {
  'use strict';

  // ---------------------------------------------------------------------------
  // Configuração (injetada pelo snippet Liquid)
  // ---------------------------------------------------------------------------
  var CONFIG = window.ShinseiFreteConfig || {};
  var API_BASE          = (CONFIG.apiBase || '').replace(/\/$/, '');
  var SUBSIDIO_POR_ITEM = CONFIG.subsidioPorItem  || 8;
  var FRETE_REAL_DEFAULT = CONFIG.freteRealDefault || 18;
  var CEP_STORAGE_KEY   = 'shinsei_frete_cep';

  // ---------------------------------------------------------------------------
  // Estado interno
  // ---------------------------------------------------------------------------
  var state = {
    cep:      localStorage.getItem(CEP_STORAGE_KEY) || '',
    qty:      1,
    peso:     0.3,
    valor:    0,
    loading:  false,
    resultado: null,
  };

  // ---------------------------------------------------------------------------
  // Utilitários de CEP
  // ---------------------------------------------------------------------------
  function formatCep(value) {
    var digits = value.replace(/\D/g, '').slice(0, 8);
    return digits.length > 5 ? digits.slice(0, 5) + '-' + digits.slice(5) : digits;
  }

  function rawCep(value) {
    return (value || '').replace(/\D/g, '');
  }

  // ---------------------------------------------------------------------------
  // Busca dados do carrinho no Shopify
  // ---------------------------------------------------------------------------
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

  // ---------------------------------------------------------------------------
  // Chamadas à API de frete
  // ---------------------------------------------------------------------------
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

  // ---------------------------------------------------------------------------
  // Atualiza a UI
  // ---------------------------------------------------------------------------
  function updateUI(data) {
    var resultado = document.getElementById('shinsei-frete-resultado');
    var barraFill = document.getElementById('shinsei-barra-fill');
    var mensagem  = document.getElementById('shinsei-frete-mensagem');
    var opcoes    = document.getElementById('shinsei-frete-opcoes');
    var erro      = document.getElementById('shinsei-frete-erro');

    if (!resultado) return;

    if (erro) erro.style.display = 'none';
    resultado.style.display = 'block';

    if (data.options !== undefined) {
      // Resposta de /frete/calcular (FreightResult)
      var cheapestReal = data.options.length > 0
        ? Math.min.apply(null, data.options.map(function (o) { return o.price_real; }))
        : FRETE_REAL_DEFAULT;
      var subsidio = data.subsidy_total || (SUBSIDIO_POR_ITEM * data.qty_items);
      var progPct  = cheapestReal > 0 ? Math.min(100, Math.round((subsidio / cheapestReal) * 100)) : 100;

      barraFill.style.width = progPct + '%';

      if (data.is_free) {
        mensagem.innerHTML = '<span class="frete-gratis">🎉 Parabéns! Você ganhou frete grátis!</span>';
        barraFill.classList.add('frete-barra-completa');
      } else {
        barraFill.classList.remove('frete-barra-completa');
        var faltam = data.items_for_free_shipping || 0;
        if (faltam === 1)      mensagem.textContent = 'Adicione 1 item para frete grátis!';
        else if (faltam > 1)   mensagem.textContent = 'Adicione ' + faltam + ' itens para frete grátis!';
        else                   mensagem.textContent = 'Quase lá! Frete com desconto aplicado.';
      }

      if (data.options && data.options.length > 0) {
        var html = '<ul class="frete-lista-opcoes">';
        data.options.forEach(function (opt) {
          var priceStr = opt.is_free
            ? '<span class="frete-gratis-tag">GRÁTIS</span>'
            : 'R$&nbsp;' + opt.price_final.toFixed(2).replace('.', ',');
          html += '<li class="frete-opcao' + (opt.is_free ? ' frete-opcao-gratis' : '') + '">';
          html += '<span class="frete-opcao-nome">'  + opt.name          + '</span>';
          html += '<span class="frete-opcao-prazo">' + opt.delivery_days + '&nbsp;dias úteis</span>';
          html += '<span class="frete-opcao-preco">' + priceStr          + '</span>';
          html += '</li>';
        });
        html += '</ul>';
        if (data.city) {
          html += '<p class="frete-cidade">📍 ' + data.city + ' - ' + data.state + '</p>';
        }
        opcoes.innerHTML = html;
      } else {
        opcoes.innerHTML = '';
      }

    } else {
      // Resposta de /frete/progresso
      var pct = data.progresso_pct || 0;
      barraFill.style.width = pct + '%';

      if (data.eh_gratis) {
        mensagem.innerHTML = '<span class="frete-gratis">🎉 Parabéns! Você ganhou frete grátis!</span>';
        barraFill.classList.add('frete-barra-completa');
      } else {
        barraFill.classList.remove('frete-barra-completa');
        mensagem.textContent = data.mensagem || 'Calcule o frete informando seu CEP.';
      }
      opcoes.innerHTML = '';
    }
  }

  function showError(msg) {
    var erro      = document.getElementById('shinsei-frete-erro');
    var resultado = document.getElementById('shinsei-frete-resultado');
    if (erro)      { erro.textContent = msg; erro.style.display = 'block'; }
    if (resultado)   resultado.style.display = 'none';
  }

  function setLoading(active) {
    var btn = document.getElementById('shinsei-cep-btn');
    if (btn) { btn.disabled = active; btn.textContent = active ? '...' : 'Calcular'; }
  }

  // ---------------------------------------------------------------------------
  // Preenche input com CEP salvo (chamado após cada re-render)
  // ---------------------------------------------------------------------------
  function fillCepInput() {
    var input = document.getElementById('shinsei-cep-input');
    if (input && state.cep) {
      input.value = formatCep(state.cep);
    }
  }

  // ---------------------------------------------------------------------------
  // Trigger de cálculo
  // ---------------------------------------------------------------------------
  function triggerCalcular() {
    var input = document.getElementById('shinsei-cep-input');
    if (!input) return;
    var cep = rawCep(input.value);
    if (cep.length !== 8) { showError('CEP inválido. Digite 8 dígitos.'); return; }
    state.cep = cep;
    localStorage.setItem(CEP_STORAGE_KEY, cep);
    update(cep);
  }

  // ---------------------------------------------------------------------------
  // Atualização principal
  // ---------------------------------------------------------------------------
  function update(forceCep) {
    if (state.loading) return;
    state.loading = true;
    setLoading(true);

    fetchCart(function (err, cart) {
      if (err || !cart) { state.loading = false; setLoading(false); return; }

      state.qty   = cart.qty;
      state.peso  = cart.peso;
      state.valor = cart.valor;

      var cep = rawCep(forceCep || state.cep || '');

      if (cep.length === 8) {
        fetchCalculo(cep, state.qty, state.peso, state.valor, function (err2, data) {
          state.loading = false;
          setLoading(false);
          if (err2 || !data) showError('Não foi possível calcular o frete. Verifique o CEP.');
          else { updateUI(data); state.resultado = data; }
        });
      } else {
        fetchProgresso(state.qty, function (err2, data) {
          state.loading = false;
          setLoading(false);
          if (err2 || !data) return; // falha silenciosa sem CEP
          updateUI(data);
          state.resultado = data;
        });
      }
    });
  }

  // ---------------------------------------------------------------------------
  // Re-inicializa após mudança no carrinho — debounce para evitar chamadas duplas
  // ---------------------------------------------------------------------------
  var _afterRenderTimer = null;
  function onAfterRender() {
    if (_afterRenderTimer) clearTimeout(_afterRenderTimer);
    _afterRenderTimer = setTimeout(function () {
      _afterRenderTimer = null;
      var cfg = window.ShinseiFreteConfig || {};
      if (cfg.apiBase)          API_BASE           = cfg.apiBase.replace(/\/$/, '');
      if (cfg.subsidioPorItem)  SUBSIDIO_POR_ITEM  = cfg.subsidioPorItem;
      if (cfg.freteRealDefault) FRETE_REAL_DEFAULT = cfg.freteRealDefault;
      state.loading = false;
      fillCepInput();
      update();
    }, 150);
  }

  // ---------------------------------------------------------------------------
  // Polling do token do carrinho — detecta mudanças de forma garantida
  // O token do Shopify muda toda vez que o carrinho é alterado
  // ---------------------------------------------------------------------------
  function startCartTokenPoll(origFetch) {
    var _lastToken = null;
    var _pollFetch = origFetch || window.fetch;  // usa fetch original, sem interceptar

    setInterval(function () {
      _pollFetch.call(window, '/cart.js')
        .then(function (r) { return r.json(); })
        .then(function (cart) {
          var token = cart.token;
          if (_lastToken !== null && token !== _lastToken) {
            // Carrinho mudou — recalcula frete após pequeno delay
            setTimeout(onAfterRender, 150);
          }
          _lastToken = token;
        })
        .catch(function () {});
    }, 900);  // verifica a cada ~1s
  }

  // ---------------------------------------------------------------------------
  // Intercepta fetch para também reagir imediatamente (complementa o poll)
  // ---------------------------------------------------------------------------
  function interceptFetch() {
    var _origFetch = window.fetch;

    window.fetch = function () {
      var args = arguments;
      var url  = typeof args[0] === 'string' ? args[0]
               : (args[0] instanceof Request ? args[0].url : '') || '';
      // Padrão: operações de carrinho do Shopify
      var isCartOp = /\/cart(\.|\/|\?|$)/i.test(url) || url.indexOf('sections=') !== -1;

      var promise = _origFetch.apply(this, args);

      if (isCartOp && url.indexOf('/cart.js') === -1) {
        // cart.js é apenas leitura — só reage a escritas (/cart/add, /cart/change, etc.)
        promise.then(function () {
          setTimeout(onAfterRender, 400);
        }).catch(function () {});
      }

      return promise;
    };

    // Inicia polling passando a referência ao fetch ORIGINAL (antes de interceptar)
    startCartTokenPoll(_origFetch);
  }

  // ---------------------------------------------------------------------------
  // Event delegation — configurado UMA vez, nunca perdido pelo re-render
  // ---------------------------------------------------------------------------
  function setupDelegation() {
    // Botão Calcular
    document.addEventListener('click', function (e) {
      if (e.target && e.target.id === 'shinsei-cep-btn') {
        triggerCalcular();
      }
    });

    // Enter no input
    document.addEventListener('keydown', function (e) {
      if (e.target && e.target.id === 'shinsei-cep-input' && e.key === 'Enter') {
        e.preventDefault();
        triggerCalcular();
      }
    });

    // Máscara de CEP
    document.addEventListener('input', function (e) {
      if (e.target && e.target.id === 'shinsei-cep-input') {
        e.target.value = formatCep(e.target.value);
      }
    });

    // Eventos padrão do Shopify / Horizon (reforço)
    document.addEventListener('shopify:section:load', function () { onAfterRender(); });
    document.addEventListener('theme:cart:change',    function () { onAfterRender(); });
    document.addEventListener('cart:updated',         function () { onAfterRender(); });
    document.addEventListener('cart:refresh',         function () { onAfterRender(); });
  }

  // ---------------------------------------------------------------------------
  // Bootstrap
  // ---------------------------------------------------------------------------
  function init() {
    if (window.ShinseiFreteInitialized) return; // evita dupla inicialização
    window.ShinseiFreteInitialized = true;
    interceptFetch();   // deve ser primeiro — guarda referência ao fetch original
    setupDelegation();
    fillCepInput();
    setTimeout(function () { update(); }, 200);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

})();
