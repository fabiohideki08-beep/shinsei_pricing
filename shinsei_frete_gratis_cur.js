/* Calculo de Frete via backend Shinsei — Shinsei Market */
(function() {
  var API_URL = 'https://shinsei-pricing.onrender.com/frete/calcular';
  var LS_CEP  = 'shinsei_cep';
  var LS_DATA = 'shinsei_frete_cache';
  var CACHE_TTL = 3600 * 1000; // 1 hora

  /* Valor minimo para frete gratis (regra de negocio client-side)
     O motor de frete no servidor nao usa o parametro valor,
     entao aplicamos o filtro aqui antes de exibir o resultado. */
  var FRETE_GRATIS_MINIMO = 29.90;

  /* Cache — chave: CEP + handle (peso varia por produto) */
  function getCache() {
    try { return JSON.parse(localStorage.getItem(LS_DATA) || '{}'); } catch(e) { return {}; }
  }
  function setCache(cache) {
    try { localStorage.setItem(LS_DATA, JSON.stringify(cache)); } catch(e) {}
  }
  function _cacheKey(cep, handle) {
    return cep + (handle ? '|' + handle : '');
  }
  function getCachedResult(cep, handle) {
    var c = getCache();
    var entry = c[_cacheKey(cep, handle)];
    if (entry && (Date.now() - entry.ts) < CACHE_TTL) return entry.data;
    return null;
  }
  function setCachedResult(cep, handle, data) {
    var c = getCache();
    c[_cacheKey(cep, handle)] = { ts: Date.now(), data: data };
    setCache(c);
  }

  /* CEP helpers */
  function getSavedCep() {
    try { return localStorage.getItem(LS_CEP) || ''; } catch(e) { return ''; }
  }
  function saveCep(cep) {
    try { localStorage.setItem(LS_CEP, cep); } catch(e) {}
  }
  function formatCep(val) {
    var d = val.replace(/\D/g, '').slice(0, 8);
    return d.length > 5 ? d.slice(0, 5) + '-' + d.slice(5) : d;
  }

  /* Le o preco do produto a partir do card.
     Tenta varios seletores comuns do tema Horizon / Shopify.
     Retorna numero em reais (ex: 19.90) ou null se nao encontrar. */
  function getPriceFromCard(card) {
    /* 1) Atributo data-price em centavos (padrao Shopify) */
    var sel = card.querySelector('[data-price]');
    if (sel) {
      var cents = parseInt(sel.getAttribute('data-price'), 10);
      if (!isNaN(cents) && cents > 0) return cents / 100;
    }

    /* 2) Elemento com atributo data-product-price */
    var dpEl = card.querySelector('[data-product-price]');
    if (dpEl) {
      var dpCents = parseInt(dpEl.getAttribute('data-product-price'), 10);
      if (!isNaN(dpCents) && dpCents > 0) return dpCents / 100;
    }

    /* 3) Preco de venda (price-item--sale) — formato "R$ 19,90" */
    var saleEl = card.querySelector('.price-item--sale, .price__sale .money, [class*="price__sale"] .money');
    if (saleEl) {
      var v = parseBRL(saleEl.textContent);
      if (v !== null) return v;
    }

    /* 4) Preco regular */
    var regEl = card.querySelector('.price-item--regular, .price__regular .money, [class*="price__regular"] .money');
    if (regEl) {
      var v2 = parseBRL(regEl.textContent);
      if (v2 !== null) return v2;
    }

    /* 5) Qualquer elemento .money dentro do card */
    var moneyEl = card.querySelector('.money');
    if (moneyEl) {
      var v3 = parseBRL(moneyEl.textContent);
      if (v3 !== null) return v3;
    }

    /* 6) money-price custom element (Horizon) */
    var mpEl = card.querySelector('money-price, price-display');
    if (mpEl) {
      var v4 = parseBRL(mpEl.textContent);
      if (v4 !== null) return v4;
    }

    return null; // nao encontrou preco
  }

  /* Converte string de preco BRL ("R$ 19,90") em float (19.90) */
  function parseBRL(str) {
    if (!str) return null;
    /* Remove tudo exceto digitos, virgula e ponto */
    var clean = str.replace(/[^0-9,\.]/g, '').trim();
    if (!clean) return null;
    /* Formato BR: "19,90" → 19.90 */
    var normalized = clean.replace(',', '.');
    var val = parseFloat(normalized);
    return (!isNaN(val) && val > 0) ? val : null;
  }

  /* Extrai o product_id Shopify de um product-card (via data-product-id) */
  function getProductIdFromCard(card) {
    return card.dataset.productId || card.getAttribute('data-product-id') || null;
  }

  /* API call — inclui product_id e valor para aplicar regra de minimo no servidor */
  function calcularFrete(cep, productId, preco, callback) {
    var cached = getCachedResult(cep, productId);
    if (cached) { callback(null, cached); return; }

    var url = API_URL + '?cep=' + cep + '&qty_items=1';
    if (productId) url += '&product_id=' + encodeURIComponent(productId);
    /* Passa o preco ao backend para que ele aplique a regra de minimo (R$19).
       Sem o preco (null) o backend recebe valor=0 e nao aplica a regra —
       nesse caso a correcao fica a cargo do aplicarRegraMinimo client-side. */
    if (preco !== null && preco > 0) url += '&valor=' + preco.toFixed(2);

    fetch(url)
      .then(function(r) {
        if (!r.ok) throw new Error('status ' + r.status);
        return r.json();
      })
      .then(function(data) {
        setCachedResult(cep, productId, data);
        callback(null, data);
      })
      .catch(function(err) {
        callback(err, null);
      });
  }

  function formatBRL(v) {
    return 'R$ ' + parseFloat(v).toFixed(2).replace('.', ',');
  }

  /* Widget creation */
  function criarWidget(card) {
    if (card.querySelector('.shinsei-frete-widget')) return;

    /* Le o preco do produto neste card para aplicar a regra do minimo */
    var preco = getPriceFromCard(card);
    var productId = getProductIdFromCard(card);

    var wrapper = document.createElement('div');
    wrapper.className = 'shinsei-frete-widget';
    wrapper.style.cssText = 'margin-top:6px;font-size:0.72rem;line-height:1.4;position:relative;z-index:20;';

    var savedCep = getSavedCep().replace(/\D/g, '');
    if (savedCep.length === 8) {
      renderLoading(wrapper);
      calcularFrete(savedCep, productId, preco, function(err, data) {
        if (err) renderInput(wrapper, productId);
        else renderResultado(wrapper, data, savedCep, preco);
      });
    } else {
      renderInput(wrapper, productId);
    }

    /* Determina ponto de insercao — SEMPRE dentro do flex container de texto,
       nunca no root <product-card> (que e um grid container e quebraria o layout). */
    var parcelas = card.querySelector('.shinsei-parcelas');
    if (parcelas && parcelas.parentElement) {
      /* 1) Apos o bloco de parcelamento (produto com installments visivel) */
      parcelas.parentElement.insertBefore(wrapper, parcelas.nextSibling);
    } else {
      /* 2) Apos o elemento de preco (produto sem parcelamento ou esgotado) */
      var priceEl = card.querySelector('product-price');
      if (priceEl && priceEl.parentElement) {
        priceEl.parentElement.insertBefore(wrapper, priceEl.nextSibling);
      } else {
        /* 3) Ao final do container de conteudo do card (flex column)
              — nunca no root <product-card> para nao quebrar o grid layout */
        var contentEl = card.querySelector('.product-card__content, .product-grid__card');
        if (contentEl) {
          contentEl.appendChild(wrapper);
        } else {
          card.appendChild(wrapper);
        }
      }
    }
  }

  function renderLoading(wrapper) {
    wrapper.innerHTML = '<span style="color:#aaa;font-size:0.68rem;">Calculando frete...</span>';
  }

  function renderInput(wrapper, productId) {
    wrapper.innerHTML = '';

    var input = document.createElement('input');
    input.type = 'text';
    input.placeholder = 'Digite seu CEP para ver o frete';
    input.maxLength = 9;
    input.className = 'shinsei-cep-input';
    input.style.cssText = [
      'border:1px solid #ddd',
      'border-radius:4px',
      'padding:4px 8px',
      'font-size:0.72rem',
      'width:100%',
      'box-sizing:border-box',
      'outline:none',
      'font-family:inherit',
      'color:#333',
      'background:#fff',
      '-webkit-appearance:none',
      'display:block'
    ].join(';');

    function verificar() {
      var cep = input.value.replace(/\D/g, '');
      if (cep.length !== 8) return;
      saveCep(cep);
      renderLoading(wrapper);
      /* Passa o preco do card atual ao backend; cada card sera re-renderizado
         com seu proprio preco apos o retorno (ver loop abaixo). */
      var cardEl = wrapper.closest('product-card') || wrapper.closest('.product-card') || wrapper;
      var precoAtual = getPriceFromCard(cardEl);
      calcularFrete(cep, productId, precoAtual, function(err, data) {
        document.querySelectorAll('product-card, .product-card').forEach(function(card) {
          var w = card.querySelector('.shinsei-frete-widget');
          if (!w) return;
          var cardProductId = getProductIdFromCard(card);
          var preco = getPriceFromCard(card);
          if (err) renderInput(w, cardProductId);
          else renderResultado(w, data, cep, preco);
        });
      });
    }

    input.addEventListener('click', function(e) { e.stopPropagation(); this.focus(); });
    input.addEventListener('touchstart', function(e) { e.stopPropagation(); }, { passive: true });
    input.addEventListener('mousedown', function(e) { e.stopPropagation(); });
    input.addEventListener('input', function() {
      this.value = formatCep(this.value);
      // Auto-submete ao completar 8 digitos (9 chars com traco)
      if (this.value.replace(/\D/g, '').length === 8) verificar();
    });
    input.addEventListener('keydown', function(e) { if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); verificar(); } });

    wrapper.appendChild(input);
  }

  function btnAlterar(cep) {
    var b = document.createElement('button');
    b.textContent = cep.slice(0, 5) + '-' + cep.slice(5) + ' ✎';
    b.title = 'Alterar CEP';
    b.style.cssText = 'background:none;border:none;color:#aaa;font-size:0.62rem;cursor:pointer;padding:0;font-family:inherit;white-space:nowrap;';
    b.addEventListener('click', function() {
      saveCep('');
      document.querySelectorAll('product-card, .product-card').forEach(function(card) {
        var w = card.querySelector('.shinsei-frete-widget');
        if (!w) return;
        renderInput(w, getProductIdFromCard(card));
      });
    });
    return b;
  }

  /* Aplica a regra de negocio: produtos abaixo do minimo nao tem frete gratis.
     Retorna uma copia do objeto data com is_free corrigido se necessario. */
  function aplicarRegraMinimo(data, preco) {
    if (!data.is_free) return data; // ja pago, nada a corrigir

    /* Se nao conseguimos ler o preco do card, tratamento conservador:
       nao exibe frete gratis para evitar erro de negocio. O backend ja
       deve ter aplicado a regra quando o valor foi passado via API;
       este fallback cobre apenas o caso de leitura de DOM falha. */
    var precoEfetivo = (preco === null) ? 0 : preco;

    if (precoEfetivo < FRETE_GRATIS_MINIMO) {
      /* Clona para nao alterar o objeto cacheado */
      var corrigido = JSON.parse(JSON.stringify(data));
      corrigido.is_free = false;
      /* Marca cada opcao como paga tambem */
      if (corrigido.options) {
        corrigido.options.forEach(function(o) {
          o.is_free = false;
          /* Se price_final foi zerado pelo subsidio, recompoe com price_real */
          if (o.price_final === 0 && o.price_real > 0) {
            o.price_final = o.price_real;
          }
        });
      }
      return corrigido;
    }
    return data;
  }

  function renderResultado(wrapper, data, cep, preco) {
    /* Aplica regra de minimo antes de renderizar */
    data = aplicarRegraMinimo(data, preco);

    wrapper.innerHTML = '';
    var col = document.createElement('div');
    col.style.cssText = 'display:flex;flex-direction:column;gap:2px;';

    if (data.is_free) {
      var badge = document.createElement('div');
      badge.style.cssText = 'display:flex;align-items:center;gap:6px;flex-wrap:wrap;';

      var tag = document.createElement('span');
      tag.textContent = 'Frete Gratis';
      tag.style.cssText = 'background:#1a7f37;color:#fff;font-size:0.7rem;font-weight:700;padding:2px 7px;border-radius:4px;';
      badge.appendChild(tag);
      badge.appendChild(btnAlterar(cep));
      col.appendChild(badge);

      /* Entrega no mesmo dia para RMSP antes das 12h (fuso SP = UTC-3) */
      var detalhe = document.createElement('div');
      detalhe.style.cssText = 'color:#1a7f37;font-size:0.65rem;font-weight:600;';
      if (data.is_rmsp) {
        var agora = new Date();
        var horaSP = agora.getUTCHours() - 3;
        if (horaSP < 0) horaSP += 24;
        var minSP = agora.getUTCMinutes();
        var antesDoMeio = horaSP < 12 || (horaSP === 12 && minSP === 0);
        if (antesDoMeio) {
          detalhe.textContent = 'Entrega hoje! (pedidos ate 12h)';
        } else {
          detalhe.textContent = 'Receba amanhã';
        }
      } else {
        /* Outras opcoes de entrega */
        var opts = data.options || [];
        detalhe.style.cssText = 'color:#888;font-size:0.63rem;';
        var nomes = opts.map(function(o) { return o.name + ' em ' + o.delivery_days + 'd'; });
        detalhe.textContent = nomes.join(' ou ');
      }
      col.appendChild(detalhe);

    } else {
      /* Frete pago — ordena por preco */
      var opts2 = (data.options || []).slice().sort(function(a, b) { return a.price_final - b.price_final; });
      var cheapest = opts2[0];

      if (!cheapest) {
        var errMsg = document.createElement('span');
        errMsg.textContent = 'Frete indisponivel para este CEP';
        errMsg.style.cssText = 'color:#999;font-size:0.68rem;';
        col.appendChild(errMsg);
      } else {
        var linha = document.createElement('div');
        linha.style.cssText = 'display:flex;align-items:center;gap:6px;flex-wrap:wrap;';

        var preco2 = document.createElement('span');
        preco2.style.cssText = 'font-size:0.72rem;color:#333;';
        preco2.textContent = 'Frete a partir de ' + formatBRL(cheapest.price_final);
        linha.appendChild(preco2);
        linha.appendChild(btnAlterar(cep));
        col.appendChild(linha);

        /* Detalhe da opcao mais barata: transportadora e prazo */
        var detalhe2 = document.createElement('div');
        detalhe2.style.cssText = 'color:#888;font-size:0.63rem;';
        detalhe2.textContent = cheapest.name + ' • ' + cheapest.delivery_days + ' dias úteis';
        col.appendChild(detalhe2);
      }
    }

    wrapper.appendChild(col);
  }

  /* Inject into all cards */
  function inject() {
    document.querySelectorAll('product-card, .product-card').forEach(criarWidget);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', inject);
  } else {
    inject();
  }

  var observer = new MutationObserver(function(mutations) {
    var relevant = mutations.some(function(m) {
      return Array.from(m.addedNodes).some(function(n) {
        return n.nodeType === 1 && (
          n.matches('product-card, .product-card') ||
          (n.querySelector && n.querySelector('product-card, .product-card'))
        );
      });
    });
    if (relevant) setTimeout(inject, 120);
  });
  observer.observe(document.body, { childList: true, subtree: true });
})();


