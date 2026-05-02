/**
 * frete-widget.js — Widget de progresso de frete para Shinsei Market
 *
 * Lógica:
 *  - Lê qtd do carrinho via /cart.js do Shopify
 *  - Se CEP informado: chama GET /frete/calcular?cep=X&qty=Y&peso=Z&valor=V
 *  - Se não: chama GET /frete/progresso?qty=Y&frete_real=18
 *  - Atualiza barra de progresso e mensagem dinamicamente
 *  - Persiste CEP no localStorage
 */

(function () {
  'use strict';

  // -------------------------------------------------------------------------
  // Configuração (injetada pelo snippet Liquid ou sobrescrita globalmente)
  // -------------------------------------------------------------------------
  var CONFIG = window.ShinseiFreteConfig || {};
  var API_BASE = (CONFIG.apiBase || '').replace(/\/$/, '');
  var SUBSIDIO_POR_ITEM = CONFIG.subsidioPorItem || 8;
  var FRETE_REAL_DEFAULT = CONFIG.freteRealDefault || 18;
  var CEP_STORAGE_KEY = 'shinsei_frete_cep';

  // -------------------------------------------------------------------------
  // Estado interno
  // -------------------------------------------------------------------------
  var state = {
    cep: localStorage.getItem(CEP_STORAGE_KEY) || '',
    qty: 1,
    peso: 0.3,
    valor: 0,
    loading: false,
    resultado: null,
  };

  // -------------------------------------------------------------------------
  // Injetar HTML do widget no DOM
  // -------------------------------------------------------------------------
  function injectWidget() {
    var target = document.getElementById('shinsei-frete-widget-container');
    if (!target) {
      // Tenta inserir antes do botão de checkout ou após a lista de itens
      var selectors = [
        '.cart__footer',
        '.cart-footer',
        '[data-cart-footer]',
        '.cart__cta',
        'form[action="/cart"]',
      ];
      for (var i = 0; i < selectors.length; i++) {
        target = document.querySelector(selectors[i]);
        if (target) break;
      }
    }
    if (!target) {
      target = document.body;
    }

    var wrapper = document.createElement('div');
    wrapper.innerHTML = [
      '<div id="shinsei-frete-widget">',
      '  <div class="frete-header">🚚 Frete</div>',
      '  <div class="frete-cep-input">',
      '    <input type="text" id="shinsei-cep-input" placeholder="Digite seu CEP" maxlength="9" autocomplete="postal-code" inputmode="numeric" />',
      '    <button id="shinsei-cep-btn" type="button">Calcular</button>',
      '  </div>',
      '  <div class="frete-resultado" id="shinsei-frete-resultado" style="display:none">',
      '    <div class="frete-barra-container">',
      '      <div class="frete-barra-fill" id="shinsei-barra-fill" style="width: 0%"></div>',
      '    </div>',
      '    <div class="frete-mensagem" id="shinsei-frete-mensagem"></div>',
      '    <div class="frete-opcoes" id="shinsei-frete-opcoes"></div>',
      '  </div>',
      '  <div class="frete-erro" id="shinsei-frete-erro" style="display:none"></div>',
      '</div>',
    ].join('\n');

    target.insertAdjacentElement('beforebegin', wrapper.firstElementChild);
  }

  // -------------------------------------------------------------------------
  // Formata CEP com máscara 00000-000
  // -------------------------------------------------------------------------
  function formatCep(value) {
    var digits = value.replace(/\D/g, '').slice(0, 8);
    if (digits.length > 5) {
      return digits.slice(0, 5) + '-' + digits.slice(5);
    }
    return digits;
  }

  function rawCep(value) {
    return value.replace(/\D/g, '');
  }

  // -------------------------------------------------------------------------
  // Busca dados do carrinho no Shopify
  // -------------------------------------------------------------------------
  function fetchCart(callback) {
    fetch('/cart.js')
      .then(function (r) { return r.json(); })
      .then(function (cart) {
        var qty = 0;
        var peso = 0;
        var valor = 0;
        var items = cart.items || [];
        for (var i = 0; i < items.length; i++) {
          var item = items[i];
          qty += item.quantity || 1;
          // Shopify envia grams por unidade
          peso += ((item.grams || 300) * (item.quantity || 1)) / 1000;
          valor += ((item.price || 0) / 100) * (item.quantity || 1);
        }
        callback(null, { qty: Math.max(1, qty), peso: Math.max(0.1, peso), valor: valor });
      })
      .catch(function (err) {
        callback(err, null);
      });
  }

  // -------------------------------------------------------------------------
  // Chamada à API de progresso (sem CEP)
  // -------------------------------------------------------------------------
  function fetchProgresso(qty, callback) {
    var url = API_BASE + '/frete/progresso?qty=' + qty + '&frete_real=' + FRETE_REAL_DEFAULT;
    fetch(url)
      .then(function (r) { return r.json(); })
      .then(function (data) { callback(null, data); })
      .catch(function (err) { callback(err, null); });
  }

  // -------------------------------------------------------------------------
  // Chamada à API de cálculo completo (com CEP)
  // -------------------------------------------------------------------------
  function fetchCalculo(cep, qty, peso, valor, callback) {
    var url = API_BASE + '/frete/calcular?cep=' + cep + '&qty=' + qty + '&peso=' + peso.toFixed(3) + '&valor=' + valor.toFixed(2);
    fetch(url)
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (data) { callback(null, data); })
      .catch(function (err) { callback(err, null); });
  }

  // -------------------------------------------------------------------------
  // Atualiza a UI
  // -------------------------------------------------------------------------
  function updateUI(data) {
    var resultado = document.getElementById('shinsei-frete-resultado');
    var barraFill = document.getElementById('shinsei-barra-fill');
    var mensagem = document.getElementById('shinsei-frete-mensagem');
    var opcoes = document.getElementById('shinsei-frete-opcoes');
    var erro = document.getElementById('shinsei-frete-erro');

    if (!resultado) return;

    erro.style.display = 'none';
    resultado.style.display = 'block';

    // Dados vindos de /frete/calcular (FreightResult)
    if (data.options !== undefined) {
      var cheapestReal = data.options.length > 0 ? Math.min.apply(null, data.options.map(function (o) { return o.price_real; })) : FRETE_REAL_DEFAULT;
      var subsidio = data.subsidy_total || (SUBSIDIO_POR_ITEM * data.qty_items);
      var progPct = cheapestReal > 0 ? Math.min(100, Math.round((subsidio / cheapestReal) * 100)) : 100;

      barraFill.style.width = progPct + '%';

      if (data.is_free) {
        mensagem.innerHTML = '<span class="frete-gratis">🎉 Parabéns! Você ganhou frete grátis!</span>';
        barraFill.classList.add('frete-barra-completa');
      } else {
        barraFill.classList.remove('frete-barra-completa');
        var faltam = data.items_for_free_shipping || 0;
        if (faltam === 1) {
          mensagem.textContent = 'Adicione 1 item para frete grátis!';
        } else if (faltam > 1) {
          mensagem.textContent = 'Adicione ' + faltam + ' itens para frete grátis!';
        } else {
          mensagem.textContent = 'Quase lá! Frete com desconto aplicado.';
        }
      }

      // Lista de opções de frete
      if (data.options && data.options.length > 0) {
        var html = '<ul class="frete-lista-opcoes">';
        data.options.forEach(function (opt) {
          var priceStr = opt.is_free
            ? '<span class="frete-gratis-tag">GRÁTIS</span>'
            : 'R$&nbsp;' + opt.price_final.toFixed(2).replace('.', ',');
          html += '<li class="frete-opcao' + (opt.is_free ? ' frete-opcao-gratis' : '') + '">';
          html += '<span class="frete-opcao-nome">' + opt.name + '</span>';
          html += '<span class="frete-opcao-prazo">' + opt.delivery_days + '&nbsp;dias úteis</span>';
          html += '<span class="frete-opcao-preco">' + priceStr + '</span>';
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
      // Dados vindos de /frete/progresso
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
    var erro = document.getElementById('shinsei-frete-erro');
    var resultado = document.getElementById('shinsei-frete-resultado');
    if (erro) {
      erro.textContent = msg;
      erro.style.display = 'block';
    }
    if (resultado) resultado.style.display = 'none';
  }

  function setLoading(active) {
    var btn = document.getElementById('shinsei-cep-btn');
    if (btn) {
      btn.disabled = active;
      btn.textContent = active ? '...' : 'Calcular';
    }
  }

  // -------------------------------------------------------------------------
  // Atualização principal: busca carrinho e então calcula frete
  // -------------------------------------------------------------------------
  function update(forceCep) {
    if (state.loading) return;
    state.loading = true;
    setLoading(true);

    fetchCart(function (err, cart) {
      if (err || !cart) {
        state.loading = false;
        setLoading(false);
        return;
      }

      state.qty = cart.qty;
      state.peso = cart.peso;
      state.valor = cart.valor;

      var cep = rawCep(forceCep || state.cep || '');

      if (cep.length === 8) {
        fetchCalculo(cep, state.qty, state.peso, state.valor, function (err2, data) {
          state.loading = false;
          setLoading(false);
          if (err2 || !data) {
            showError('Não foi possível calcular o frete. Verifique o CEP.');
          } else {
            updateUI(data);
            state.resultado = data;
          }
        });
      } else {
        fetchProgresso(state.qty, function (err2, data) {
          state.loading = false;
          setLoading(false);
          if (err2 || !data) {
            // Falha silenciosa para progresso sem CEP
            return;
          }
          updateUI(data);
          state.resultado = data;
        });
      }
    });
  }

  // -------------------------------------------------------------------------
  // Inicialização de eventos
  // -------------------------------------------------------------------------
  function bindEvents() {
    var input = document.getElementById('shinsei-cep-input');
    var btn = document.getElementById('shinsei-cep-btn');

    if (!input || !btn) return;

    // Preenche com CEP salvo
    if (state.cep) {
      input.value = formatCep(state.cep);
    }

    // Máscara ao digitar
    input.addEventListener('input', function () {
      var formatted = formatCep(this.value);
      this.value = formatted;
    });

    // Enter no campo
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        triggerCalcular();
      }
    });

    // Botão calcular
    btn.addEventListener('click', function () {
      triggerCalcular();
    });
  }

  function triggerCalcular() {
    var input = document.getElementById('shinsei-cep-input');
    if (!input) return;
    var cep = rawCep(input.value);
    if (cep.length !== 8) {
      showError('CEP inválido. Digite 8 dígitos.');
      return;
    }
    state.cep = cep;
    localStorage.setItem(CEP_STORAGE_KEY, cep);
    update(cep);
  }

  // -------------------------------------------------------------------------
  // Escuta eventos de atualização do carrinho do Shopify
  // -------------------------------------------------------------------------
  function listenCartEvents() {
    // Shopify Theme Events
    document.addEventListener('cart:updated', function () {
      update();
    });

    // Mutation observer para mudanças de quantidade
    var cartForms = document.querySelectorAll('form[action="/cart"]');
    cartForms.forEach(function (form) {
      form.addEventListener('change', function () {
        setTimeout(function () { update(); }, 500);
      });
    });

    // Observa mudanças no contador do carrinho (ex: badge)
    var cartCountSelectors = [
      '[data-cart-count]',
      '.cart-count',
      '.cart__count',
      '#CartCount',
    ];
    for (var i = 0; i < cartCountSelectors.length; i++) {
      var el = document.querySelector(cartCountSelectors[i]);
      if (el) {
        var observer = new MutationObserver(function () {
          setTimeout(function () { update(); }, 300);
        });
        observer.observe(el, { childList: true, subtree: true, characterData: true });
        break;
      }
    }
  }

  // -------------------------------------------------------------------------
  // Bootstrap
  // -------------------------------------------------------------------------
  function init() {
    injectWidget();
    bindEvents();
    listenCartEvents();
    // Primeira atualização
    setTimeout(function () { update(); }, 200);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
